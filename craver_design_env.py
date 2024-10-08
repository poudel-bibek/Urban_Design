import random
import gymnasium as gym
import numpy as np
import xml.etree.ElementTree as ET

class CraverDesignEnv(gym.Env):
    """
    For the higher level agent, modifies the net file based on the design decision.
    No need to connect or close this environment. Will be limited to network file modifications.
    """
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.original_net_file = './SUMO_files/original_craver_road.net.xml'


    @property
    def action_space(self):
        """
        """
        return gym.spaces.MultiDiscrete([2] * 15)


    @property
    def observation_space(self):
        """
        """
        return gym.spaces.Box(low=0, high=1, shape=(10, 74), dtype=np.float32)


    def step(self, action):
        """
        """
        pass
    

    def _apply_action(self, action):
        """
        """
        pass
    
    def _get_reward(self, action):
        """
        """
        pass

    def reset(self):
        """
        """
        pass

    def close(self):
        """
        Probably wont make use of it, just for completeness.
        """
        pass

    def _modify_net_file(self, crosswalks_to_disable):
        """
        Just for changing the appearence of disallowed crosswalks. Not used right now.
        """
        tree = ET.parse(self.original_net_file)
        root = tree.getroot()

        for crosswalk_id in crosswalks_to_disable:
            # Find the edge element corresponding to this crosswalk
            edge = root.find(f".//edge[@id='{crosswalk_id}']")
            if edge is not None:
                # Find the lane within the crosswalk
                lane = edge.find('lane')
                if lane is not None:
                    lane.set('width', '0.1')

        tree.write('./SUMO_files/modified_craver_road.net.xml')

# This should be done here before the SUMO call. This can disallow pedestrians before the simulation run.
# Randomly select crosswalks to disable
# to_disable = random.sample(self.controlled_crosswalks, min(5, len(self.controlled_crosswalks)))
# Before sumo call 
# self._modify_net_file(to_disable)