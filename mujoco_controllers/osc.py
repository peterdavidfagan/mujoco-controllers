"""
Operational Space Controller

Heavily inspired by Kevin Zakka's implementation: https://github.com/kevinzakka/mjctrl/blob/main/opspace.py
"""

from typing import Tuple, Dict, Optional, Union, List

import numpy as np
from scipy.spatial.transform import Rotation as R
import pandas as pd

import mujoco
from mujoco import viewer

from dm_control import composer, mjcf
from dm_robotics.moma.models.arenas import empty
from dm_robotics.moma import robot
from dm_robotics.transformations.transformations import mat_to_quat, quat_to_mat, quat_to_euler
from dm_robotics.transformations import transformations as tr

from mujoco_controllers.build_env import construct_physics

from rearrangement_benchmark.env_components.props import Rectangle
from ikpy.chain import Chain
from hydra import compose, initialize
from hydra.utils import instantiate

class OSC(object):

    def __init__(self, physics, passive_view, arm, controller_config):
        # core simulation instance
        self.physics = physics
        self.passive_view = passive_view

        # controller gains
        self.controller_gains = controller_config["gains"]
        self.position_threshold = controller_config["convergence"]["position_threshold"]
        self.orientation_threshold = controller_config["convergence"]["orientation_threshold"]
        
        # get site and actuator details from arm
        self.eef_site = arm.attachment_site
        self.arm_joints = arm.joints
        self.arm_joint_ids = np.array(physics.bind(self.arm_joints).dofadr)

        # control targets
        self._eef_target_position = None
        self._eef_target_velocity = None
        self._eef_target_quat = None
        self._eef_target_angular_velocity = None
        self._gripper_status = "open"
        
        # control equation variables
        self._eef_mass_matrix = None
        self._eef_jacobian = None

    @property
    def eef_target_position(self):
        return self._eef_target_position
    
    @eef_target_position.setter
    def eef_target_position(self, value):
        self._eef_target_position = value

    @property
    def eef_target_quat(self):
        return self._eef_target_quat

    @eef_target_quat.setter
    def eef_target_quat(self, value):
        self._eef_target_quat = value
    
    @property
    def eef_target_velocity(self):
        return self._eef_target_velocity

    @eef_target_velocity.setter
    def eef_target_velocity(self, value):
        self._eef_target_velocity = value

    @property
    def eef_target_angular_velocity(self):
        return self._eef_target_angular_velocity

    @eef_target_angular_velocity.setter
    def eef_target_angular_velocity(self, value):
        self._eef_target_angular_velocity = value

    def _compute_eef_mass_matrix(self):
        nv = self.physics.model.nv
        M = np.zeros((nv, nv))
        mujoco.mj_fullM(self.physics.model.ptr, M, self.physics.data.qM)
        M = M[self.arm_joint_ids, :][:, self.arm_joint_ids]
    
        M_inv = np.linalg.inv(M)
        Mx_inv = np.dot(self._eef_jacobian, np.dot(M_inv, self._eef_jacobian.T))
        if abs(np.linalg.det(Mx_inv)) >= 1e-2:
            # do the linalg inverse if matrix is non-singular
            # because it's faster and more accurate
            self._eef_mass_matrix = np.linalg.inv(Mx_inv)
        else:
            # using the rcond to set singular values < thresh to 0
            # singular values < (rcond * max(singular_values)) set to 0
            self._eef_mass_matrix = np.linalg.pinv(Mx_inv, rcond=1e-2)

    def _compute_eef_jacobian(self):
        nv = self.physics.model.nv
        jacp = np.zeros((3, nv))
        jacr = np.zeros((3, nv))
        mujoco.mj_jacSite(m=self.physics.model.ptr, d=self.physics.data.ptr, jacp=jacp, jacr=jacr, site=self.physics.bind(self.eef_site).element_id)
        jacp = jacp[:, self.arm_joint_ids] # filter jacobian for joints we care about
        jacr = jacr[:, self.arm_joint_ids] # filter jacobian for joints we care about
        self._eef_jacobian = np.vstack([jacp, jacr])
    
    def _orientation_error(
        self,
        quat: np.ndarray,
        quat_des: np.ndarray,
    ) -> np.ndarray:
        quat_err = tr.quat_mul(quat, tr.quat_conj(quat_des))
        quat_err /= np.linalg.norm(quat_err)
        axis_angle = tr.quat_to_axisangle(quat_err)
        if quat_err[0] < 0.0:
            angle = np.linalg.norm(axis_angle) - 2 * np.pi
        else:
            angle = np.linalg.norm(axis_angle)
        return axis_angle * angle

    def _calc_damping(self, gains: Dict[str, float]) -> np.ndarray:
        return gains["damping_ratio"] * 2 * np.sqrt(gains["kp"])

    def pd_control(
        self,
        x: np.array,
        x_desired: np.array,
        dx: np.array,
        dx_desired: np.array,
        gains:Tuple,
        mode="position"):
    
        if mode == "position":
            try:
                gains = self.controller_gains["position"]
            except:
                raise ValueError("Invalid controller gains")
            error = gains["kp"] * (x_desired - x) + self._calc_damping(gains)  * (dx_desired - dx)
            # considered limiting error term
            return error
            
        elif mode == "orientation": 
            try:
                gains = self.controller_gains["orientation"]
            except:
                raise ValueError("Invalid controller gains")
            error = gains["kp"] * self._orientation_error(x, x_desired) + self._calc_damping(gains) * (dx_desired - dx) 
            return error
        else:
            raise ValueError("Invalid mode for pd control")
    
    def compute_control_output(self):
        """ Compute the control output for the robot arm. """
        # update control member variables
        self._compute_eef_jacobian()
        self._compute_eef_mass_matrix()

        # get joint velocities
        current_joint_velocity = physics.data.qvel[self.arm_joint_ids]
        
        # calculate position pd control
        eef_current_position = physics.bind(self.eef_site).xpos.copy()
        eef_current_velocity = self._eef_jacobian[:3,:] @ current_joint_velocity
        position_pd = self.pd_control(
            x=eef_current_position,
            x_desired=self._eef_target_position,
            dx=eef_current_velocity,
            dx_desired=self._eef_target_velocity,
            gains=self.controller_gains["position"],
            mode="position"
        )
        
        # calculate orientation pd control
        eef_quat = mat_to_quat(physics.bind(self.eef_site).xmat.reshape(3,3).copy())
        eef_angular_vel = self._eef_jacobian[3:,:] @ current_joint_velocity
        orientation_pd = self.pd_control(
            x=eef_quat,
            x_desired=self._eef_target_quat,
            dx=eef_angular_vel,
            dx_desired=self._eef_target_angular_velocity,
            gains=self.controller_gains["orientation"],
            mode="orientation"
                )
        
        pd_error = np.hstack([position_pd, orientation_pd])
        tau = self._eef_jacobian.T @ self._eef_mass_matrix @ pd_error 
        tau += self.physics.data.qfrc_bias[self.arm_joint_ids]
        
        # compute effective torque
        actuator_moment_inv = np.linalg.pinv(self.physics.data.actuator_moment)
        actuator_moment_inv = actuator_moment_inv[self.arm_joint_ids, :][:, self.arm_joint_ids]
        tau = tau @ actuator_moment_inv 

        if self._gripper_status == "open":
            tau = np.concatenate([tau, [0.0]])
        else:
            tau = np.concatenate([tau, [255.0]])

        return tau

    def run_controller(self, duration):
        converged = False
        start_time = self.physics.data.time
        while (self.physics.data.time - start_time < duration) and (not converged):
            control_command = self.compute_control_output()
            self.physics.set_control(control_command)
            self.physics.step()
            self.passive_view.sync()
    
            # TODO: move to separate function
            # check if we have converged
            eef_pos = physics.bind(self.eef_site).xpos.copy()
            eef_quat = mat_to_quat(physics.bind(self.eef_site).xmat.reshape(3,3).copy())
            eef_vel = self._eef_jacobian[:3,:] @ physics.data.qvel[self.arm_joint_ids]
            eef_angular_vel = self._eef_jacobian[3:,:] @ physics.data.qvel[self.arm_joint_ids]
            position_error = np.linalg.norm(eef_pos - self._eef_target_position)
            orientation_error = tr.quat_dist(eef_quat, self._eef_target_quat)

            # TODO: add velocity convergence
            if (position_error < self.position_threshold) and (orientation_error < self.orientation_threshold):
                converged = True


if __name__ == "__main__":
    
    # save default configs 
    ready_config = np.array([0, -0.785, 0, -2.356, 0, 1.571, 0.785])
    grasp_pose_config = np.array([-3.95380744e-04,  2.37985323e-01,  3.52180384e-04, -2.55912981e+00,
     -2.42755642e-04,  2.79711454e+00,  7.85573570e-01])

    # load different robot configurations
    initialize(version_base=None, config_path="./config", job_name="default_config")
    POSITION_CONFIG = compose(config_name="controller_tuning", overrides=["robots=default"])
    VELOCITY_CONFIG = compose(config_name="controller_tuning", overrides=["robots=velocity"])
    MOTOR_CONFIG = compose(config_name="controller_tuning", overrides=["robots=motor"])
    IKPY_URDF_PATH = "./models/arms/robot.urdf"

    # For now assign default cfg
    cfg = MOTOR_CONFIG
    kinematic_chain = Chain.from_urdf_file(IKPY_URDF_PATH, base_elements=["panda_link0"]) 

    physics, passive_view, arm, gripper = construct_physics(cfg)

    # run the controller
    osc = OSC(physics, passive_view, arm, MOTOR_CONFIG["controllers"]["osc"])
    
    # compute the eef targets
    target_eef_pose = np.array([0.45,0.0,0.6])
    target_orientation = R.from_euler('xyz', [0, 180, 0], degrees=True).as_matrix()
    target_quat = mat_to_quat(target_orientation)

    # above target
    osc.eef_target_position = target_eef_pose
    osc.eef_target_velocity = np.array([0.0, 0.0, 0.0])
    osc.eef_target_quat = target_quat
    osc.eef_target_angular_velocity = 0.0
    osc.run_controller(1.0)

    # pregrasp pose
    osc.eef_target_position = target_eef_pose - np.array([0.0, 0.0, 0.4])
    osc.run_controller(1.0)

