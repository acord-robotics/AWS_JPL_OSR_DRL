from __future__ import print_function

import time
import boto3
import gym
import numpy as np
from gym import spaces
import PIL
from PIL import Image
import os
import random
import math
import sys
import rospy
from nav_msgs.msg import Odometry
from geometry_msgs.msg import Twist, Pose, Quaternion
from gazebo_msgs.srv import SetModelState, SetModelConfiguration
from gazebo_msgs.msg import ModelState, ContactsState
from sensor_msgs.msg import Image as sensor_image
from sensor_msgs.msg import LaserScan, Imu
from geometry_msgs.msg import Point
from std_msgs.msg import Float64
from std_msgs.msg import String
from PIL import Image
import queue


VERSION = "0.0.1"
TRAINING_IMAGE_WIDTH = 160
TRAINING_IMAGE_HEIGHT = 120
TRAINING_IMAGE_SIZE = (TRAINING_IMAGE_WIDTH, TRAINING_IMAGE_HEIGHT)

LIDAR_SCAN_MAX_DISTANCE = 4.5  # Max distance Lidar scanner can measure
CRASH_DISTANCE = 0.49  # Min distance to obstacle (The LIDAR is in the center of the 1M Rover)

# Size of the image queue buffer, we want this to be one so that we consume 1 image
# at a time, but may want to change this as we add more algorithms
IMG_QUEUE_BUF_SIZE = 1

# Prevent unknown "stuck" scenarios with a kill switch (MAX_STEPS)
MAX_STEPS = 2000

# Destination Point
CHECKPOINT_X = -44.25
CHECKPOINT_Y = -4

PREVIOUS_IMU = 0
AVG_IMU = 0

# Initial position of the robot
INITIAL_POS_X = -0.170505086911
INITIAL_POS_Y = 0.114341186761
INITIAL_POS_Z = -0.0418765865136

INITIAL_ORIENT_X = 0.0135099011407
INITIAL_ORIENT_Y = 0.040927747122
INITIAL_ORIENT_Z = 0.0365547169101
INITIAL_ORIENT_W = 0.998401800258


# Initial distance to checkpoint
INITIAL_DISTANCE_TO_CHECKPOINT = abs(math.sqrt(((CHECKPOINT_X - INITIAL_POS_X) ** 2) +
                                               ((CHECKPOINT_Y - INITIAL_POS_Y) ** 2)))


# SLEEP INTERVALS - a buffer to give Gazebo, RoS and the rl_agent to sync.
SLEEP_AFTER_RESET_TIME_IN_SECOND = 0.3
SLEEP_BETWEEN_ACTION_AND_REWARD_CALCULATION_TIME_IN_SECOND = 0.3 # LIDAR Scan is 5 FPS (0.2sec).
SLEEP_WAITING_FOR_IMAGE_TIME_IN_SECOND = 0.01

#Custom Global variables
reached_waypoint_4 = False
reached_waypoint_5 = False
reached_waypoint_6 = False


class MarsEnv(gym.Env):
    def __init__(self):
        self.x = INITIAL_POS_X                                                  # Current position of Rover
        self.y = INITIAL_POS_Y                                                  # Current position of Rover
        self.last_position_x = INITIAL_POS_X                                    # Previous position of Rover
        self.last_position_y = INITIAL_POS_Y                                    # Previous position of Rover
        #self.orientation = None
        self.aws_region = os.environ.get("AWS_REGION", "us-east-1")             # Region for CloudWatch Metrics
        self.reward_in_episode = 0                                              # Global episodic reward variable
        self.steps = 0                                                          # Global episodic step counter
        self.collision_threshold = 4.5   #was sys.maxsize                           # current collision distance
        self.last_collision_threshold = 4.5  # sys.maxsize                      # previous collision distance
        self.collision = False                                                  # Episodic collision detector
        self.distance_travelled = 0                                             # Global episodic distance counter
        self.current_distance_to_checkpoint = INITIAL_DISTANCE_TO_CHECKPOINT    # current distance to checkpoint
        self.closer_to_checkpoint = False                                       # Was last step closer to checkpoint?
        self.state = None                                                       # Observation space
        self.steering = 0
        self.throttle = 0
        self.power_supply_range = MAX_STEPS                                     # Kill switch (power supply)


        # Imu Sensor readings
        self.max_lin_accel_x = 0
        self.max_lin_accel_y = 0
        self.max_lin_accel_z = 0

        self.reached_waypoint_1 = False
        self.reached_waypoint_2 = False
        self.reached_waypoint_3 = False

        # action space -> steering angle, throttle
        self.action_space = spaces.Box(low=np.array([-1, 0]), high=np.array([+1, +3]), dtype=np.float32)


        # Create the observation space
        self.observation_space = spaces.Box(low=0, high=255,
                                            shape=(TRAINING_IMAGE_SIZE[1], TRAINING_IMAGE_SIZE[0], 3),
                                            dtype=np.uint8)

        self.image_queue = queue.Queue(IMG_QUEUE_BUF_SIZE)

        # ROS initialization
        self.ack_publisher = rospy.Publisher('/cmd_vel', Twist, queue_size=100)

        # ROS Subscriptions
        self.current_position_pub = rospy.Publisher('/current_position', Point, queue_size=3)
        self.distance_travelled_pub = rospy.Publisher('/distance_travelled', String, queue_size=3)
        # ################################################################################

        # Gazebo model state
        self.gazebo_model_state_service = rospy.ServiceProxy('/gazebo/set_model_state', SetModelState)
        self.gazebo_model_configuration_service = rospy.ServiceProxy('/gazebo/set_model_configuration', SetModelConfiguration)
        rospy.init_node('rl_coach', anonymous=True)

        # Subscribe to ROS topics and register callbacks
        rospy.Subscriber('/odom', Odometry, self.callback_pose)
        rospy.Subscriber('/scan', LaserScan, self.callback_scan)
        rospy.Subscriber('/robot_bumper', ContactsState, self.callback_collision)
        rospy.Subscriber('/camera/image_raw', sensor_image, self.callback_image)
        # IMU Sensors
        rospy.Subscriber('/imu/wheel_lb', Imu, self.callback_wheel_lb)



    '''
    DO NOT EDIT - Function called by rl_coach to instruct the agent to take an action
    '''
    def step(self, action):
        # initialize rewards, next_state, done
        self.reward = None
        self.done = False
        self.next_state = None

        steering = float(action[0])
        throttle = float(action[1])
        self.steps += 1
        self.send_action(steering, throttle)
        time.sleep(SLEEP_BETWEEN_ACTION_AND_REWARD_CALCULATION_TIME_IN_SECOND)

        self.call_reward_function(action)

        info = {}  # additional data, not to be used for training

        return self.next_state, self.reward, self.done, info


    '''
    DO NOT EDIT - Function called at the conclusion of each episode to reset episodic values
    '''
    def reset(self):
        print('Total Episodic Reward=%.2f' % self.reward_in_episode,
              'Total Episodic Steps=%.2f' % self.steps)
        self.send_reward_to_cloudwatch(self.reward_in_episode)

        # Reset global episodic values
        self.reward = None
        self.done = False
        self.next_state = None
        self.ranges= None
        self.send_action(0, 0) # set the throttle to 0
        self.rover_reset()
        self.call_reward_function([0, 0])

        return self.next_state


    '''
    DO NOT EDIT - Function called to send the agent's chosen action to the simulator (Gazebo)
    '''
    def send_action(self, steering, throttle):
        speed = Twist()
        speed.linear.x = throttle
        speed.angular.z = steering
        self.ack_publisher.publish(speed)


    '''
    DO NOT EDIT - Function to reset the rover to the starting point in the world
    '''
    def rover_reset(self):

        # Reset Rover-related Episodic variables
        rospy.wait_for_service('gazebo/set_model_state')

        self.x = INITIAL_POS_X
        self.y = INITIAL_POS_Y

        # Put the Rover at the initial position
        model_state = ModelState()
        model_state.pose.position.x = INITIAL_POS_X
        model_state.pose.position.y = INITIAL_POS_Y
        model_state.pose.position.z = INITIAL_POS_Z
        model_state.pose.orientation.x = INITIAL_ORIENT_X
        model_state.pose.orientation.y = INITIAL_ORIENT_Y
        model_state.pose.orientation.z = INITIAL_ORIENT_Z
        model_state.pose.orientation.w = INITIAL_ORIENT_W
        model_state.twist.linear.x = 0
        model_state.twist.linear.y = 0
        model_state.twist.linear.z = 0
        model_state.twist.angular.x = 0
        model_state.twist.angular.y = 0
        model_state.twist.angular.z = 0
        model_state.model_name = 'rover'

        # List of joints to reset (this is all of them)
        joint_names_list = ["rocker_left_corner_lb",
                             "rocker_right_corner_rb",
                             "body_rocker_left",
                             "body_rocker_right",
                             "rocker_right_bogie_right",
                             "rocker_left_bogie_left",
                             "bogie_left_corner_lf",
                             "bogie_right_corner_rf",
                             "corner_lf_wheel_lf",
                             "imu_wheel_lf_joint",
                             "bogie_left_wheel_lm",
                             "imu_wheel_lm_joint",
                             "corner_lb_wheel_lb",
                             "imu_wheel_lb_joint",
                             "corner_rf_wheel_rf",
                             "imu_wheel_rf_joint",
                             "bogie_right_wheel_rm",
                             "imu_wheel_rm_joint",
                             "corner_rb_wheel_rb",
                             "imu_wheel_rb_joint"]
        # Angle to reset joints to
        joint_positions_list = [0 for _ in range(len(joint_names_list))]

        self.gazebo_model_state_service(model_state)
        self.gazebo_model_configuration_service(model_name='rover', urdf_param_name='rover_description', joint_names=joint_names_list, joint_positions=joint_positions_list)

        self.last_collision_threshold = self.collision_threshold #fix instead of  sys.maxsize
        self.last_position_x = self.x
        self.last_position_y = self.y

        time.sleep(SLEEP_AFTER_RESET_TIME_IN_SECOND)

        self.distance_travelled = 0
        self.current_distance_to_checkpoint = INITIAL_DISTANCE_TO_CHECKPOINT
        self.steps = 0
        self.reward_in_episode = 0
        self.collision = False
        self.closer_to_checkpoint = False
        self.power_supply_range = MAX_STEPS
        self.reached_waypoint_1 = False
        self.reached_waypoint_2 = False
        self.reached_waypoint_3 = False
        self.max_lin_accel_x = 0
        self.max_lin_accel_y = 0
        self.max_lin_accel_z = 0

        # First clear the queue so that we set the state to the start image
        _ = self.image_queue.get(block=True, timeout=None)
        self.set_next_state()

    '''
    DO NOT EDIT - Function to find the distance between the rover and nearest object within 4.5M via LIDAR
    '''
    def get_distance_to_object(self):

        while not self.ranges:
            time.sleep(SLEEP_WAITING_FOR_IMAGE_TIME_IN_SECOND)

        size = len(self.ranges)
        x = np.linspace(0, size - 1, 360)
        xp = np.arange(size)
        val = np.clip(np.interp(x, xp, self.ranges), 0, LIDAR_SCAN_MAX_DISTANCE)
        val[np.isnan(val)] = LIDAR_SCAN_MAX_DISTANCE

        # Find min distance
        self.collision_threshold = np.amin(val)


    '''
    DO NOT EDIT - Function to resize the image from the camera and set observation_space
    '''
    def set_next_state(self):
        try:
            # Make sure the first image is the starting image
            image_data = self.image_queue.get(block=True, timeout=None)

            # Read the image and resize to get the state
            image = Image.frombytes('RGB', (image_data.width, image_data.height), image_data.data, 'raw', 'RGB', 0, 1)
            image = image.resize((TRAINING_IMAGE_WIDTH,TRAINING_IMAGE_HEIGHT), PIL.Image.ANTIALIAS)

            # TODO - can we crop this image to get additional savings?

            self.next_state = np.array(image)
        except Exception as err:
            print("Error!::set_next_state:: {}".format(err))


    '''
    DO NOT EDIT - Reward Function buffer
    '''


    def call_reward_function(self, action):
        self.get_distance_to_object() #<-- Also evaluate for sideswipe and collistion damage

        # Get the observation
        self.set_next_state()

        # reduce power supply range
        self.power_supply_range = MAX_STEPS - self.steps

        # Get average Imu reading
        if self.max_lin_accel_x > 0 or self.max_lin_accel_y > 0 or self.max_lin_accel_z > 0:
            avg_imu = (self.max_lin_accel_x + self.max_lin_accel_y + self.max_lin_accel_y) / 3
            AVG_IMU = avg_imu
        else:
            avg_imu = 0

        # calculate reward
        reward, done = self.reward_function()

        # Accumulate reward for the episode
        self.reward_in_episode += reward

        print('Step:%.2f' % self.steps,
              'Steering:%f' % action[0],
              'R:%.4f' % reward,                                # Reward
              'DTCP:%f' % self.current_distance_to_checkpoint,  # Distance to Check Point
              'X:%.2f' % self.x,                                # X
              'Y:%.2f' % self.y,                                # Y
              'DT:%f' % self.distance_travelled,                # Distance Travelled
              'CT:%.2f' % self.collision_threshold,             # Collision Threshold
              'CTCP:%f' % self.closer_to_checkpoint,            # Is closer to checkpoint
              'PSR: %f' % self.power_supply_range,              # Steps remaining in Episode
              'IMU: %f' % avg_imu)


        self.reward = reward
        self.done = done

        self.last_position_x = self.x
        self.last_position_y = self.y
        self.last_collision_threshold = self.collision_threshold

    '''
    EDIT - but do not change the function signature.
    Must return a reward value as a float
    Must return a boolean value indicating if episode is complete
    Must be returned in order of reward, done
    '''

    def reward_function(self):
        import gc
        '''
        :return: reward as float
                 done as boolean
        '''
        #Keep track of waypoint progress
        global reached_waypoint_4
        global reached_waypoint_5
        global reached_waypoint_6
        
        # Corner boundaries of the world (in Meters)
        STAGE_X_MIN = -47.0
        STAGE_Y_MIN = -25.0
        STAGE_X_MAX = 15.0
        STAGE_Y_MAX = 22.0

        # checkpoint -44.25,-4

        GUIDERAILS_X_MIN = -48
        GUIDERAILS_X_MAX = 1
        GUIDERAILS_Y_MIN = -8
        GUIDERAILS_Y_MAX = 6


        # WayPoints to checkpoint
        WAYPOINT_1_X = -10
        WAYPOINT_1_Y = -4

        WAYPOINT_2_X = -34 #-17
        WAYPOINT_2_Y = -3   #3

        WAYPOINT_3_X = -34
        WAYPOINT_3_Y = 3

        # REWARD Multipliers
        FINISHED_REWARD = 10000
        WAYPOINT_1_REWARD = 1000
        WAYPOINT_2_REWARD = 2000
        WAYPOINT_3_REWARD = 3000
        WAYPOINT_4_REWARD = 4000
        WAYPOINT_5_REWARD = 5000
        WAYPOINT_6_REWARD = 6000

        reward = 0
        base_reward = 2
        multiplier = 0
        done = False

        distance = math.sqrt((self.x - self.last_position_x)**2 + (self.y - self.last_position_y)**2)
        dist_increment = round(distance,2)

        def reset_progress():
            reached_waypoint_4 = False
            reached_waypoint_5 = False
            reached_waypoint_6 = False
            

        if self.steps > 0:

            # Check for episode ending events first
            # ###########################################

            # Has LIDAR registered a hit
            if self.collision_threshold <= CRASH_DISTANCE + 0.50:   # Eliminates the stuck wheel episodes  < 0.99
                print("Rover has sustained sideswipe damage")
                return 0, True # No reward

            # if the rover is stuck and not moving
            if dist_increment < 0.01 and self.distance_travelled > 10 :
                print("Rover seems to be stuck on a rock. Distance inc", dist_increment)
                return 0, True # No reward

            # Have the gravity sensors registered too much G-force
            if self.collision:
                print("Rover has collided with an object")
                return 0, True # No reward

            # Has the rover reached the max steps
            if self.power_supply_range < 1:
                print("Rover's power supply has been drained (MAX Steps reached")
                return 0, True # No reward

            # Has the Rover reached the destination
            if self.last_position_x <= CHECKPOINT_X and self.last_position_y <= CHECKPOINT_Y:
                print("Congratulations! The rover has reached the checkpoint!")
                multiplier = FINISHED_REWARD
                reward = (base_reward * multiplier) / self.steps # <-- incentivize to reach checkpoint in fewest steps
                return reward, True

            # If it has not reached the check point is it still on the map?
            if self.x < (GUIDERAILS_X_MIN - .45) or self.x > (GUIDERAILS_X_MAX + .45):
                print("Rover has left the mission map!")
                return 0, True


            if self.y < (GUIDERAILS_Y_MIN - .45) or self.y > (GUIDERAILS_Y_MAX + .45):
                print("Rover has left the mission map!")
                return 0, True

            # No Episode ending events - continue to calculate reward
            # smooth reward based on distance
            multiplier = (1 - (self.current_distance_to_checkpoint/INITIAL_DISTANCE_TO_CHECKPOINT)**4 ) * 5

            #waypoints
            progress = INITIAL_DISTANCE_TO_CHECKPOINT / self.current_distance_to_checkpoint

            if progress >=1.3 and progress <1.7:
                # Determine if Rover already received one time reward for reaching this waypoint
                if not self.reached_waypoint_1:  
                    self.reached_waypoint_1 = True
                    print("Congratulations! The rover has reached waypoint 1!")
                    multiplier = 1 
                    reward = (WAYPOINT_1_REWARD * multiplier * 10) / self.steps # <-- incentivize to reach way-point in fewest steps
                    reset_progress()  #reset other later progress meters after first waypoint
                    return reward, False
                    
            if progress >=1.7 and progress <2:
                # Determine if Rover already received one time reward for reaching this waypoint
                if not self.reached_waypoint_2:  
                    self.reached_waypoint_2 = True
                    print("Congratulations! The rover has reached waypoint 2!")
                    multiplier = 1 
                    reward = (WAYPOINT_2_REWARD * multiplier * 10) / self.steps # <-- incentivize to reach way-point in fewest steps
                    return reward, False
                    
            if progress >=2 and progress <3:
                # Determine if Rover already received one time reward for reaching this waypoint
                if not self.reached_waypoint_3:  
                    self.reached_waypoint_3 = True
                    print("Congratulations! The rover has reached waypoint 3!")
                    multiplier = 1 
                    reward = (WAYPOINT_3_REWARD * multiplier * 10) / self.steps # <-- incentivize to reach way-point in fewest steps
                    return reward, False
                    
            if progress >=3 and progress <4:
                # Determine if Rover already received one time reward for reaching this waypoint
                if not reached_waypoint_4:
                    reached_waypoint_4 = True
                    print("Congratulations! The rover has reached waypoint 4!") 
                    multiplier = 1 
                    reward = (WAYPOINT_4_REWARD * multiplier * 10) / self.steps # <-- incentivize to reach way-point in fewest steps
                    return reward, False
            
            if progress >=4 and progress <5:
                # Determine if Rover already received one time reward for reaching this waypoint
                if not reached_waypoint_5:
                    reached_waypoint_5 = True
                    print("Congratulations! The rover has reached waypoint 5!")
                    multiplier = 1 
                    reward = (WAYPOINT_5_REWARD * multiplier * 10)  / self.steps # <-- incentivize to reach way-point in fewest steps
                    return reward, False
            
            if progress >=7 and progress <8:
                # Determine if Rover already received one time reward for reaching this waypoint
                if  not reached_waypoint_6:
                    reached_waypoint_6 = True
                    print("Congratulations! The rover has reached waypoint 6!")
                    multiplier = 1 
                    reward = (WAYPOINT_6_REWARD * multiplier * 10 )  / self.steps # <-- incentivize to reach way-point in fewest steps
                    return reward, False

            # To reach this point in the function the Rover has either not yet reached the way-points OR has already gotten the one time reward for reaching the waypoint(s)

            # less sparse collision threshold:
            multiplier = multiplier + (self.collision_threshold / 2.0) - 0.5

            # Get average Imu reading
            if self.max_lin_accel_x > 0 or self.max_lin_accel_y > 0 or self.max_lin_accel_z > 0:
                avg_imu = (self.max_lin_accel_x + self.max_lin_accel_y + self.max_lin_accel_y) / 3
            else:
                avg_imu = 0

            # Incentivize the rover to stay on smooth surfaces from objects - helps with smoother drive
            if avg_imu  < 5.0:
                multiplier = multiplier + 1
            elif avg_imu < 8.0 and avg_imu >= 5.0: # pretty safe
                multiplier = multiplier + .5
            elif avg_imu < 9.0 and avg_imu >= 8.0: # just enough time to turn
                multiplier = multiplier + .25
            else:
                multiplier = multiplier # rough terrain


            #Penalize heavily for  going away from destination or going over rough terrain
            if (self.closer_to_checkpoint == False):
                if multiplier > 0:
                    multiplier = multiplier / 2

            print('LCT:%.2f' % self.last_collision_threshold,   # Last Collision Threshold
              'X:%.2f' % self.x,                                # X
              'Y:%.2f' % self.y,                                # Y
              'LX:%.2f' % self.last_position_x,                 # Previous X
              'LY:%.2f' % self.last_position_y,                 # Previous
              'DI:%.2f' % dist_increment,                       # Distance Increment
              'MULT:%.4f' % multiplier,                          # Multiplier
              'AVG IMU:%.4f' % avg_imu                          # Average IMU
              )

            reward = (base_reward * multiplier )

            gc.collect()

        return reward, done





    '''
    DO NOT EDIT - Function to receive LIDAR data from a ROSTopic
    '''
    def callback_scan(self, data):
        self.ranges = data.ranges


    '''
    DO NOT EDIT - Function to receive image data from the camera RoSTopic
    '''
    def callback_image(self, data):
        try:
             self.image_queue.put_nowait(data)
        except queue.Full:
            pass
        except Exception as ex:
           print("Error! {}".format(ex))


    '''
    DO NOT EDIT - Function to receive IMU data from the Rover wheels
    '''
    def callback_wheel_lb(self, data):
        lin_accel_x = data.linear_acceleration.x
        lin_accel_y = data.linear_acceleration.x
        lin_accel_z = data.linear_acceleration.x

        if lin_accel_x > self.max_lin_accel_x:
            self.max_lin_accel_x = lin_accel_x

        if lin_accel_y > self.max_lin_accel_y:
            self.max_lin_accel_y = lin_accel_y

        if lin_accel_z > self.max_lin_accel_z:
            self.max_lin_accel_z = lin_accel_z



    '''
    DO NOT EDIT - Function to receive Position/Orientation data from a ROSTopic
    '''
    def callback_pose(self, data):
        #self.orientation = data.pose.pose.orientation
        self.linear_trajectory = data.twist.twist.linear
        self.angular_trajectory = data.twist.twist.angular

        new_position = data.pose.pose.position

        p = Point(new_position.x, new_position.y, new_position.z)

        # Publish current position
        self.current_position_pub.publish(p)

        # Calculate total distance travelled
        dist = math.hypot(new_position.x - self.x, new_position.y - self.y)
        self.distance_travelled += dist

        # Calculate the distance to checkpoint
        new_distance_to_checkpoint = Float64
        new_distance_to_checkpoint.data = abs(math.sqrt(((new_position.x - CHECKPOINT_X) ** 2) +
                                                        (new_position.y - CHECKPOINT_Y) ** 2))

        if new_distance_to_checkpoint.data < self.current_distance_to_checkpoint:
            self.closer_to_checkpoint = True
        else:
            self.closer_to_checkpoint = False

        # Update the distance to checkpoint
        self.current_distance_to_checkpoint = new_distance_to_checkpoint.data

        # update the current position
        self.x = new_position.x
        self.y = new_position.y


    '''
    DO NOT EDIT - Function to receive Collision data from a ROSTopic
    '''
    def callback_collision(self, data):
        # Listen for a collision with anything in the environment
        collsion_states = data.states
        if len(collsion_states) > 0:
            self.collide = True


    '''
    DO NOT EDIT - Function to wrote episodic rewards to CloudWatch
    '''
    def send_reward_to_cloudwatch(self, reward):
        try:
            session = boto3.session.Session()
            cloudwatch_client = session.client('cloudwatch', region_name=self.aws_region)
            cloudwatch_client.put_metric_data(
                MetricData=[
                    {
                        'MetricName': 'Episode_Reward',
                        'Unit': 'None',
                        'Value': reward
                    },
                    {
                        'MetricName': 'Episode_Steps',
                        'Unit': 'None',
                        'Value': self.steps,
                    },
                    {
                        'MetricName': 'DistanceToCheckpoint',
                        'Unit': 'None',
                        'Value': self.current_distance_to_checkpoint
                    }
                ],
                Namespace='AWS_NASA_JPL_OSR_Challenge'
            )
        except Exception as err:
            print("Error in the send_reward_to_cloudwatch function: {}".format(err))


'''
DO NOT EDIT - Inheritance class to convert discrete actions to continuous actions
'''
class MarsDiscreteEnv(MarsEnv):
    def __init__(self):
        MarsEnv.__init__(self)
        print("New Martian Gym environment created...")

        # actions -> straight, left, right
        self.action_space = spaces.Discrete(3)

    def step(self, action):
        # Convert discrete to continuous
        if action == 0:  # turn left
            steering = 1.0
            throttle = 3.00
        elif action == 1:  # turn right
            steering = -1.0
            throttle = 3.00
        elif action == 2:  # straight
            steering = 0
            throttle = 3.00
        else:  # should not be here
            raise ValueError("Invalid action")

        continuous_action = [steering, throttle]

        return super().step(continuous_action)
        