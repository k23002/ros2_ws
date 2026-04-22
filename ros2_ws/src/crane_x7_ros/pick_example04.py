#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from moveit_msgs.msg import PlanningScene, CollisionObject, Constraints, JointConstraint, AttachedCollisionObject
from moveit_msgs.action import MoveGroup, ExecuteTrajectory
from moveit_msgs.srv import GetCartesianPath, GetPositionIK
from shape_msgs.msg import SolidPrimitive
from geometry_msgs.msg import Pose
from control_msgs.action import GripperCommand
from builtin_interfaces.msg import Duration
from tf2_ros import Buffer, TransformListener
import time
import math
import copy

class CoordinatePickAndPlace(Node):
    def __init__(self):
        super().__init__('coordinate_pick_py')
        
        self.scene_pub = self.create_publisher(PlanningScene, '/planning_scene', 10)
        self.move_group_client = ActionClient(self, MoveGroup, 'move_action')
        self.gripper_client = ActionClient(self, GripperCommand, '/crane_x7_gripper_controller/gripper_cmd')
        self.compute_path_client = self.create_client(GetCartesianPath, 'compute_cartesian_path')
        self.execute_client = ActionClient(self, ExecuteTrajectory, 'execute_trajectory')
        
        self.ik_client = self.create_client(GetPositionIK, 'compute_ik')
        
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        
        self.joint_names = [
            'crane_x7_shoulder_fixed_part_pan_joint',
            'crane_x7_shoulder_revolute_part_tilt_joint',
            'crane_x7_upper_arm_revolute_part_twist_joint',
            'crane_x7_upper_arm_revolute_part_rotate_joint',
            'crane_x7_lower_arm_fixed_part_joint',
            'crane_x7_lower_arm_revolute_part_rotate_joint',
            'crane_x7_wrist_joint'
        ]
        
    def euler_to_quaternion(self, roll, pitch, yaw):
        qx = math.sin(roll/2) * math.cos(pitch/2) * math.cos(yaw/2) - math.cos(roll/2) * math.sin(pitch/2) * math.sin(yaw/2)
        qy = math.cos(roll/2) * math.sin(pitch/2) * math.cos(yaw/2) + math.sin(roll/2) * math.cos(pitch/2) * math.sin(yaw/2)
        qz = math.cos(roll/2) * math.cos(pitch/2) * math.sin(yaw/2) - math.sin(roll/2) * math.sin(pitch/2) * math.cos(yaw/2)
        qw = math.cos(roll/2) * math.cos(pitch/2) * math.cos(yaw/2) + math.sin(roll/2) * math.sin(pitch/2) * math.sin(yaw/2)
        return [qx, qy, qz, qw]

    def spawn_fixed_box(self, box_x, box_y, box_z):
        self.get_logger().info('Spawning the target box in RViz (Hologram)...')
        scene_msg = PlanningScene()
        scene_msg.is_diff = True
        
        box = CollisionObject()
        box.header.frame_id = 'base_link'
        box.id = 'target_box'
        prim_box = SolidPrimitive()
        prim_box.type = SolidPrimitive.BOX
        
        # 5cm角の黒い箱にサイズを合わせる
        prim_box.dimensions = [0.05, 0.05, 0.05]
        
        pose_box = Pose()
        pose_box.position.x = box_x
        pose_box.position.y = box_y
        pose_box.position.z = box_z 
        
        box.primitives.append(prim_box)
        box.primitive_poses.append(pose_box)
        box.operation = CollisionObject.ADD

        scene_msg.world.collision_objects = [box]
        for _ in range(3):
            self.scene_pub.publish(scene_msg)
            time.sleep(0.5)

    def attach_target_box(self, attach=True):
        scene_msg = PlanningScene()
        scene_msg.is_diff = True
        aco = AttachedCollisionObject()
        aco.object.id = 'target_box'
        aco.link_name = 'crane_x7_gripper_base_link' 
        
        if attach:
            aco.object.operation = CollisionObject.ADD
            aco.touch_links = [
                'crane_x7_gripper_base_link',
                'crane_x7_gripper_finger_a_link',
                'crane_x7_gripper_finger_b_link'
            ]
        else:
            aco.object.operation = CollisionObject.REMOVE
            
        scene_msg.robot_state.attached_collision_objects = [aco]
        for _ in range(3):
            self.scene_pub.publish(scene_msg)
            time.sleep(0.2)

    def control_gripper(self, open_ratio):
        angle = math.radians(45) * open_ratio
        goal = GripperCommand.Goal()
        goal.command.position = angle
        goal.command.max_effort = 100.0
        
        self.gripper_client.wait_for_server()
        future = self.gripper_client.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, future)
        res_future = future.result().get_result_async()
        rclpy.spin_until_future_complete(self, res_future)
        time.sleep(1.0)

    def move_joint(self, joint_values):
        goal = MoveGroup.Goal()
        goal.request.group_name = 'arm'
        goal.request.max_velocity_scaling_factor = 0.5
        goal.request.max_acceleration_scaling_factor = 0.5
        goal.planning_options.plan_only = False
        goal.planning_options.replan = True
        goal.planning_options.replan_attempts = 3
        
        constraints = Constraints()
        for name, val in zip(self.joint_names, joint_values):
            jc = JointConstraint()
            jc.joint_name = name
            jc.position = val
            jc.tolerance_above = 0.05 
            jc.tolerance_below = 0.05
            jc.weight = 1.0
            constraints.joint_constraints.append(jc)
        goal.request.goal_constraints.append(constraints)
        
        self.move_group_client.wait_for_server()
        send_goal_future = self.move_group_client.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, send_goal_future)
        goal_handle = send_goal_future.result()
        
        if not goal_handle.accepted:
            return False
            
        res_future = goal_handle.get_result_async()
        rclpy.spin_until_future_complete(self, res_future)
        res = res_future.result().result
        
        time.sleep(1.0)
        return res.error_code.val == 1

    def move_to_pose(self, target_pose):
        self.get_logger().info(f"Calculating IK for X:{target_pose.position.x:.2f}, Y:{target_pose.position.y:.2f}, Z:{target_pose.position.z:.2f}")
        
        req = GetPositionIK.Request()
        req.ik_request.group_name = 'arm'
        req.ik_request.pose_stamped.header.frame_id = 'base_link'
        req.ik_request.pose_stamped.pose = target_pose
        
        self.ik_client.wait_for_service()
        future = self.ik_client.call_async(req)
        rclpy.spin_until_future_complete(self, future)
        res = future.result()
        
        if res.error_code.val == 1: 
            arm_joints = []
            for name, pos in zip(res.solution.joint_state.name, res.solution.joint_state.position):
                if name in self.joint_names:
                    arm_joints.append(pos)
            
            self.get_logger().info("IK calculation successful! Moving arm...")
            return self.move_joint(arm_joints)
        else:
            self.get_logger().error(f"IK Failed! Error Code: {res.error_code.val}")
            return False

    def get_current_pose(self):
        target_frame = 'base_link'
        source_frame = 'crane_x7_gripper_base_link'
        for i in range(20):
            rclpy.spin_once(self, timeout_sec=0.1)
            try:
                if self.tf_buffer.can_transform(target_frame, source_frame, rclpy.time.Time()):
                    trans = self.tf_buffer.lookup_transform(target_frame, source_frame, rclpy.time.Time())
                    pose = Pose()
                    pose.position.x = trans.transform.translation.x
                    pose.position.y = trans.transform.translation.y
                    pose.position.z = trans.transform.translation.z
                    pose.orientation = trans.transform.rotation
                    return pose
            except Exception:
                pass
            time.sleep(0.2)
        return None

    def move_straight_z(self, distance_z_world, ignore_collisions=False):
        curr_pose = self.get_current_pose()
        if not curr_pose:
            return False

        req = GetCartesianPath.Request()
        req.header.frame_id = 'base_link'
        req.group_name = 'arm'
        
        p = copy.deepcopy(curr_pose)
        p.position.z += distance_z_world 
        
        req.waypoints = [p]
        req.max_step = 0.01
        req.avoid_collisions = not ignore_collisions

        future = self.compute_path_client.call_async(req)
        rclpy.spin_until_future_complete(self, future)
        resp = future.result()
        
        if resp.fraction < 0.9:
            self.get_logger().error(f'Straight move planning failed: fraction={resp.fraction}')
            return False

        time_per_point = 0.15
        for i, pt in enumerate(resp.solution.joint_trajectory.points):
            t = (i + 1) * time_per_point
            pt.time_from_start = Duration(sec=int(t), nanosec=int((t % 1) * 1e9))
            
        goal = ExecuteTrajectory.Goal()
        goal.trajectory = resp.solution
        
        self.execute_client.wait_for_server()
        future = self.execute_client.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, future)
        res_future = future.result().get_result_async()
        rclpy.spin_until_future_complete(self, res_future)
        
        time.sleep(1.0)
        
        error_code = res_future.result().result.error_code.val
        if error_code != 1:
            self.get_logger().error(f'Straight move execution failed! Error code: {error_code}')
            return False
            
        return True

    def run(self):
        # =========================================================
        # ★ 1. 箱の座標と、斜めアプローチのための三角関数計算
        # =========================================================
        box_x = 0.30
        box_y = 0.0
        box_z = 0.025  # 5cmの箱の中心高さ
        
        tcp_offset_z = 0.035
        approach_distance = 0.15  # 15cm上空（安全かつ関節に余裕がある高さ）
        
        # ★ 新規：真下(180度)ではなく、関節に優しい160度に設定
        tilt_angle_rad = math.radians(170)
        
        # 垂直からのズレ角度（180度 - 160度 = 20度）
        angle_diff = math.pi - tilt_angle_rad
        
        # 手首が斜めになるため、指先が箱の中心に合うように手首の目標座標をズラす
        wrist_offset_x = tcp_offset_z * math.sin(angle_diff)
        wrist_offset_z = tcp_offset_z * math.cos(angle_diff)

        # 現実と同じく、最初から箱は存在している状態にする
        self.spawn_fixed_box(box_x, box_y, box_z)

        self.get_logger().info('Opening gripper...')
        self.control_gripper(1.0)

        # =========================================================
        # ★ Step 0 & 0.5: 準備姿勢
        # =========================================================
        self.get_logger().info('Step 0: Moving to Home position...')
        home_joints = [0.0, -0.5, 0.0, -1.0, 0.0, -0.5, 0.0]
        if not self.move_joint(home_joints): return

        self.get_logger().info('Step 0.5: Moving to Intermediate Posture (Ready to grasp)...')
        """
        # 手首のひねり(最後から2番目と最後)を素直な状態にしておく
        ready_joints = [0.0, -0.5, 0.0, -1.5, 0.0, -1.14, 0.0]
        if not self.move_joint(ready_joints): return
	"""

        # =========================================================
        # ★ Step 1: オフセットを考慮した座標・角度へ移動
        # =========================================================
        self.get_logger().info('Step 1: Moving to Target Coordinates (Pre-grasp)...')
        
        target_pose = Pose()
        # ★ 変更：指先を合わせるため、手首の位置を少し手前(マイナス側)に引く
        target_pose.position.x = box_x - wrist_offset_x
        target_pose.position.y = box_y
        # 高さは 箱の高さ ＋ オフセットZ ＋ アプローチ距離
        target_pose.position.z = box_z + wrist_offset_z + approach_distance
        
        # Pitchに160度を指定（Yaw=0.0 で横掴み）
        q = self.euler_to_quaternion(0.0, tilt_angle_rad, 0.0)
        target_pose.orientation.x = q[0]
        target_pose.orientation.y = q[1]
        target_pose.orientation.z = q[2]
        target_pose.orientation.w = q[3]

        if not self.move_to_pose(target_pose): return
        time.sleep(1.0)

        # =========================================================
        # ★ Step 2〜5: アプローチ、掴む、持ち上げ、帰還
        # =========================================================
        self.get_logger().info(f'Step 2: Approaching the box...')
        if not self.move_straight_z(-approach_distance, ignore_collisions=True): return

        self.get_logger().info('Step 3: Grasping the box (Side grasp)...')
        # ガッチリ掴むために少し深めに閉じる
        self.control_gripper(0.48) 
        time.sleep(1.5)
        self.attach_target_box(attach=True)

        self.get_logger().info('Step 4: Lifting the box (Moving UP 7cm)...')
        self.move_straight_z(0.07, ignore_collisions=False)

        self.get_logger().info('Step 5: Returning to Home position...')
        self.move_joint(home_joints)
        
        self.get_logger().info('*** Coordinate Pick and Place Completed! ***')
        
def main(args=None):
    rclpy.init(args=args)
    node = CoordinatePickAndPlace()
    node.run()
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
