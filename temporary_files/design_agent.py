import os
import json
import wandb
wandb.require("core") # Bunch of improvements in using the core.

import traci
import queue
import torch
import random
import numpy as np
import torch.multiprocessing as mp

from torch.utils.tensorboard import SummaryWriter
from datetime import datetime
from control_env import ControlEnv
from design_env import DesignEnv
from models import CNNActorCritic, GATv2ActorCritic

from ppo_alg import PPO, Memory
from utils import create_new_sumocfg

def save_config(config, SEED, model, save_path):
    """
    Save hyperparameters and model architecture to a JSON file.
    """
    config_to_save = {
        "hyperparameters": config,
        "global_seed": SEED,
        "model_architecture": {
            "actor": str(model.policy.actor),
            "critic": str(model.policy.critic)
        }
    }
    
    with open(save_path, 'w') as f:
        json.dump(config_to_save, f, indent=4)

def evaluate_controller(config, env, best_network_iteration):
    """
    For benchmarking.
    Evaluate either the traffic light or PPO as the controller.
    TODO: Make the evaluation N number of times each with different seeds. Report average results.

    Give the best network iteration
    """

    create_new_sumocfg(best_network_iteration)

    # Collect step data separated at each landmarks such as TL lights
    step_data = []
    if config['evaluate'] == 'tl':
        tl_ids = env.tl_ids
        phases = env.phases

        # Figure out the cycle lengths for each tl 
        cycle_lengths = {}
        for tl_id in tl_ids:
            phase = phases[tl_id]
            cycle_lengths[tl_id]  = sum([state['duration'] for state in phase])
        
        if config['auto_start']:
            sumo_cmd = ["sumo-gui" if config['gui'] else "sumo", 
                        "--start" , 
                        "--quit-on-end", 
                        "-c", "./SUMO_files/iterative_craver.sumocfg", 
                        '--step-length', str(config['step_length'])]
                            
        else:
            sumo_cmd = ["sumo-gui" if config['gui'] else "sumo", 
                        "--quit-on-end", 
                        "-c", "./SUMO_files/iterative_craver.sumocfg", 
                        '--step-length', str(config['step_length'])]
        
        traci.start(sumo_cmd)
        env.sumo_running = True
        env._initialize_lanes()

        # Now run the sim till the horizon
        for t in range(config['max_timesteps']):
            for tl_id in tl_ids:

                # using t, determine where in the cycle we are
                current_pos_in_cycle = t % cycle_lengths[tl_id]

                # Find the index/ state
                state_index = 0
                for state in phases[tl_id]:
                    current_pos_in_cycle -= state['duration']
                    if current_pos_in_cycle < 0:
                        break
                    state_index += 1

                # Set the state
                state_string = phases[tl_id][state_index]['state']
                traci.trafficlight.setRedYellowGreenState(tl_id, state_string)

            # This is outside the loop
            traci.simulationStep()

            # After the simulation step 
            occupancy_map = env._get_occupancy_map()
            corrected_occupancy_map = env._step_operations(occupancy_map, print_map=True, cutoff_distance=100)
            step_info, all_directions = collect_step_data(t, corrected_occupancy_map, env)

            print(f"Step: {t}, step info: {step_info}")
            step_data.append(step_info)

    elif config['evaluate'] == 'ppo':
        if config['model_path']:
            # device = torch.device("cuda:0" if torch.cuda.is_available() and config['gpu'] else "cpu")
            # Maybe we should use only CPU during evaluation
            device = torch.device("cpu")

            action_dim = env.action_space.n
            n_channels = 1 
            model_kwargs = {
                'action_duration': env.observation_space.shape[0],  
                'per_timestep_state_dim': env.observation_space.shape[1], 
                'model_size': config['model_size'],  
                'kernel_size': config['kernel_size'],
                'dropout_rate': config['dropout_rate']
                }
            
            # TODO, too many device usages. fix
            ppo_model = CNNActorCritic(n_channels, action_dim, device, **model_kwargs).to(device) 
            ppo_model.load_state_dict(torch.load(config['model_path'], map_location=device)) 

            state, _ = env.reset()
            for t in range(config['max_timesteps']):
                action, _ = ppo_model.act(state)
                state, reward, done, truncated, info = env.step(action)
                
                occupancy_map = env._get_occupancy_map()
                corrected_occupancy_map = env._step_operations(occupancy_map, print_map=False, cutoff_distance=100)

                step_info, all_directions = collect_step_data(t, corrected_occupancy_map, env)
                step_data.append(step_info)

                if done or truncated:
                    break

        else:
            print("Model path not provided. Cannot evaluate PPO.")
            return None

    else: 
        print("Invalid evaluation mode. Please choose either 'tl' or 'ppo'.")
        return None
    
    return step_data, all_directions

def collect_step_data(step, occupancy_map, env):
    """
    Collect detailed data for a single step using the occupancy map.
    A vehicle is considered to be waiting (in a queue) if the velocity is less than 0.5 m/s.

    Avreage waiting time: On average, how long does a vehicle wait while crossing the intersection? 
    """

    step_info = {'step': step}
    all_directions = [f"{direction}-{turn}" for direction in env.directions for turn in env.turns]
    
    for tl_id, tl_data in occupancy_map.items():
        step_info[tl_id] = {
            'vehicle': {
                'queue_length': {direction: 0 for direction in all_directions},
                'total_outgoing': [] , # Total vehicles that crossed the intersection. Per step. From one step to the next, there might be repitition. Needs to be filtered later.
            },
            'pedestrian': {}
        }

    # Collect vehicle IDs and data for each traffic light
    for tl_id, tl_data in occupancy_map.items():

        # For queue, process both incoming and inside vehicles
        for movement_direction in tl_data['vehicle'].keys():  

            if movement_direction in ['incoming', 'inside']:
                for lane_group, ids in tl_data['vehicle'][movement_direction].items():

                    for veh_id in ids:
                        veh_velocity = traci.vehicle.getSpeed(veh_id)

                        # Increment queue length if vehicle is waiting
                        if veh_velocity < 1.0:
                            # Ensure the lane_group exists in our queue_length dictionary
                            if lane_group in step_info[tl_id]['vehicle']['queue_length']:

                                step_info[tl_id]['vehicle']['queue_length'][lane_group] += 1
                            else:
                                print(f"Warning: Unexpected lane group '{lane_group}' encountered.")

            # For total outgoing vehicles
            else: 
                for _, vehicles in tl_data['vehicle']['outgoing'].items():
                    step_info[tl_id]['vehicle']['total_outgoing'].extend(vehicles)

    return step_info, all_directions

def calculate_performance(run_data, all_directions, step_length):
    """
    Calculate the performance metrics from the run data.
    1. Average Waiting Time: For every outgoing vehicle, on average what is the waiting time?
    2. Average Queue Length: For every direction (12 total), on average what is the queue length? Counted whenever there is a queue.
    3. Overall Average Queue Length: Average queue length across all directions.
    4. Throughput: Number of vehicles per hour that crossed the intersection.
    """

    total_waiting_time = 0
    unique_outgoing_vehicles = set()
    queue_lengths = {direction: [] for direction in all_directions}
    
    # Process each step's data
    for step_info in run_data:
        for tl_id, tl_data in step_info.items():
            if tl_id != 'step':  # Skip the 'step' key
                # Collect unique outgoing vehicle IDs
                unique_outgoing_vehicles.update(tl_data['vehicle']['total_outgoing'])
                
                # Sum queue lengths
                for direction, length in tl_data['vehicle']['queue_length'].items():
                    if length > 0:
                        queue_lengths[direction].append(length)
                        total_waiting_time += length # Each waiting vehicle contributes 1 timestep of waiting time

    total_outgoing_vehicles = len(unique_outgoing_vehicles)
    
    # Calculate average waiting time using the actual step length
    total_simulation_waiting_time = total_waiting_time * step_length # In seconds
    avg_waiting_time = total_simulation_waiting_time / total_outgoing_vehicles 
    
    avg_queue_lengths = {direction: sum(lengths) / len(lengths) if lengths else 0
                         for direction, lengths in queue_lengths.items()}
    
    all_queue_lengths = [length for lengths in queue_lengths.values() for length in lengths]
    overall_avg_queue_length = sum(all_queue_lengths) / len(all_queue_lengths) if all_queue_lengths else 0

    total_simulation_time = (run_data[-1]['step'] * step_length)/ 3600  # Convert to hours
    throughput = total_outgoing_vehicles / total_simulation_time # Vehicles per hour
    
    # Print results
    print("\nPerformance Metrics:")
    print(f"Total Unique Outgoing Vehicles: {total_outgoing_vehicles}")
    print(f"Average Waiting Time: {avg_waiting_time:.2f} seconds")
    print(f"Throughput: {throughput:.2f} vehicles/hour")
    print(f"Overall Average Queue Length: {overall_avg_queue_length:.2f}")
    print("\nAverage Queue Lengths by Direction:")
    for direction, avg_length in avg_queue_lengths.items():
        print(f"  {direction}: {avg_length:.2f}")



def train(train_config, is_sweep=False, sweep_config=None):
    """
    Actors are parallelized i.e., create their own instance of the environment and interact with it (perform policy rollout).
    All aspects of training are centralized.
    Auto tune hyperparameters using wandb sweeps.

    Although Adam maintains an independent and changing lr for each policy parameter, there are still potential benefits of having a lr schedule
    Annealing is a special form of scheduling where the learning rate may not strictly decrease 

    TODO: For evaluation of a sweep, currently we are looking at reward (and not the traffic related metrics). 
    In the future, evals can be added here. i.e., evaluate the policy and then calculate the waiting time, queue length, throughput etc. metrics

    # Move towards two stage learning setup.
    # 1. Higher-level agent makes the design decisions.
    # 2. Lower-level agent makes the traffic control decisions.
    """

    SEED = train_config['seed'] if train_config['seed'] else random.randint(0, 1000000)
    print(f"Random seed: {SEED}")

    # Set global seed
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(SEED)

    if is_sweep: #DO NOT MOVE THIS BELOW. # Update train_config with wandb sweep_config
        for key, value in sweep_config.items():
            if key in train_config:
                train_config[key] = value
        
    global_step = 0
    worker_device = torch.device("cuda") if config['gpu'] and torch.cuda.is_available() else torch.device("cpu")
    print(f"Using device: {worker_device}")

    design_args = {
        'save_graph_images': True,
        'save_gmm_plots': True,
        'max_proposals': train_config['max_proposals'],
        'min_thickness': train_config['min_thickness'],
        'max_thickness': train_config['max_thickness'],
        'min_coordinate': train_config['min_coordinate'],
        'max_coordinate': train_config['max_coordinate'],
        'original_net_file': train_config['original_net_file'],
        }
    
    # Dummy environments. Required for setup.
    environments = {
        'lower': ControlEnv(train_config, worker_id=None),
        'higher': DesignEnv(design_args)
        }
    
    for env_type, env in environments.items():
        print(f"\nEnvironment for {env_type} level agent:")
        print(f"\tDefined observation space: {env.observation_space}")
        print(f"\tObservation space shape: {env.observation_space.shape}")
        print(f"\tDefined action space: {env.action_space}")
        
        if env_type == 'lower':
            print(f"\tOptions per action dimension: {env.action_space.nvec}")
        elif env_type == 'higher':
            print(f"\tNumber of proposals: {env.action_space['num_proposals'].n}")
            print(f"\tProposal space: {env.action_space['proposals']}")

    # Higher level agent
    higher_in_channels = train_config['higher_in_channels']
    higher_edge_dim = train_config['higher_edge_dim']
    higher_action_dim = design_args['max_proposals']

    higher_model_kwargs = {
        'hidden_channels': train_config['higher_hidden_channels'],
        'out_channels': train_config['higher_out_channels'],
        'initial_heads': train_config['higher_initial_heads'],
        'second_heads': train_config['higher_second_heads'],
        'edge_dim': higher_edge_dim,
        'action_hidden_channels': train_config['higher_action_hidden_channels'],
        'gmm_hidden_dim': train_config['higher_gmm_hidden_dim'],
        'num_mixtures': train_config['higher_num_mixtures'],
        }

    print(f"\nHigher level agent: \n\tIn channels: {higher_in_channels}, Action dimension: {higher_action_dim}\n")
    # Lower level agent
    state_dim = environments['lower'].observation_space.shape # e.g., (10, 74) = (action_duration, per_timestep_state_dim)
    in_channels = 1
    model_kwargs_lower = {
        'action_duration': train_config['action_duration'],  
        'per_timestep_state_dim': environments['lower'].observation_space.shape[1],  
        'model_size': train_config['lower_model_size'],  
        'kernel_size': train_config['lower_kernel_size'],
        'dropout_rate': train_config['lower_dropout_rate']
        }
        
    lower_action_dim = train_config['lower_action_dim'] 
    print(f"\nLower level agent: \n\tState dimension: {state_dim}, Action dimension: {lower_action_dim}")
    # Dont need these anymore
    environments['lower'].close() 
    environments['higher'].close() 

    higher_ppo = PPO(
        higher_in_channels,
        higher_action_dim, 
        worker_device,
        lr=train_config['higher_lr'],
        gamma=train_config['higher_gamma'],
        K_epochs=train_config['higher_K_epochs'],
        eps_clip=train_config['higher_eps_clip'],
        ent_coef=train_config['higher_ent_coef'],
        vf_coef=train_config['higher_vf_coef'],
        batch_size=train_config['higher_batch_size'],
        gae_lambda=train_config['higher_gae_lambda'],
        agent_type="higher",
        **higher_model_kwargs
        )

    lower_ppo = PPO(in_channels, 
        lower_action_dim, 
        worker_device, 
        train_config['lower_lr'], 
        train_config['lower_gamma'], 
        train_config['lower_K_epochs'], 
        train_config['lower_eps_clip'], 
        train_config['lower_ent_coef'], 
        train_config['lower_vf_coef'], 
        train_config['lower_batch_size'],
        train_config['lower_gae_lambda'],
        agent_type="lower",
        **model_kwargs_lower
        )

    if not is_sweep: 
        # TensorBoard setup
        # No need to save the model during sweep.
        current_time = datetime.now().strftime('%b%d_%H-%M-%S')
        log_dir = os.path.join('runs', current_time)
        os.makedirs('runs', exist_ok=True)
        writer = SummaryWriter(log_dir=log_dir)

        # Save hyperparameters and model architecture
        config_path = os.path.join(log_dir, f'config_{current_time}.json')
        save_config(train_config, SEED, lower_ppo, config_path)
        print(f"Configuration saved to {config_path}")

        # Model saving setup
        save_dir = os.path.join('saved_models', current_time)
        os.makedirs(save_dir, exist_ok=True)
        best_reward = float('-inf')

    # Initialize higher level environment and get initial state
    higher_env = DesignEnv(design_args)
    higher_state = higher_env.reset().to(worker_device)
    higher_memory = Memory()

    # Instead of using total_episodes, we will use total_iterations. 
    # Every iteration, num_process lower level agents interact with the environment for total_action_timesteps_per_episode steps (which further internally contains action_duration steps)
    # Each iteration is equivalent to a single timestep for the higher agent.
    total_iterations = train_config['total_timesteps'] // (train_config['max_timesteps'] * train_config['lower_num_processes'])
    lower_ppo.total_iterations = total_iterations # For lr annealing
    train_config['total_action_timesteps_per_episode'] = train_config['max_timesteps'] // train_config['action_duration'] 
    
    # Counter to keep track of how many times action has been taken by all workers (lower level agent)
    action_timesteps = 0
    for iteration in range(1, total_iterations + 1): # Starting from 1 to prevent policy update in the very first iteration.
        
        global_step = iteration * train_config['lower_num_processes']*train_config['total_action_timesteps_per_episode']*train_config['action_duration']
        print(f"\nStarting iteration: {iteration}/{total_iterations} with {global_step} total steps so far\n")

        print(f"Higher state: {higher_state}, device: {higher_state.x.device}")

        # Higher level agent takes node features, edge index, edge attributes and batch (to make single large graph) as input 
        # To produce padded fixed-sized actions, num_actual_proposals and total_log_prob are also returned.
        higher_action, num_proposals, higher_logprob = higher_ppo.policy_old.act(higher_state.x, 
                                                                                higher_state.edge_index, 
                                                                                higher_state.edge_attr, 
                                                                                None) # Only 1 graph is used to make inference at a time (for batch)
        
        #TODO: Next state needs processing.
        # Should all the actions of lower agent take place within the step of the higher agent?
        higher_next_state, _, higher_done, _ = higher_env.step(higher_action, iteration)

        # Lower-level agents get a new memory and manager each iteration.
        manager = mp.Manager() # Manager to handle shared objects
        memory_queue = manager.Queue()

        processes = []
        for rank in range(train_config['lower_num_processes']):
            p = mp.Process(target=worker, args=(rank, train_config, lower_ppo.policy_old, memory_queue, SEED, worker_device, iteration)) # Create a process to execute the worker function
            p.start()
            processes.append(p)

        if train_config['lower_anneal_lr']:
            current_lr = lower_ppo.update_learning_rate(iteration)

        all_memories = []
        active_workers = set(range(train_config['lower_num_processes']))

        while active_workers:
            try:
                rank, memory = memory_queue.get(timeout=60)  # Add a timeout to prevent infinite waiting
                
                if memory is None:
                    active_workers.remove(rank)
                else:
                    all_memories.append(memory)
                    print(f"Memory from worker {rank} received. Memory size: {len(memory.states)}")

                    # Look at the size of the memory and update action_timesteps
                    action_timesteps += len(memory.states)

                    # Update lower level PPO every n times action has been taken
                    if action_timesteps % train_config['lower_update_freq'] == 0:
                        loss = lower_ppo.update(all_memories, agent_type='lower')

                        total_reward = sum(sum(memory.rewards) for memory in all_memories)
                        avg_reward = total_reward / train_config['lower_num_processes'] # Average reward per process in this iteration
                        print(f"\nAverage Reward per process: {avg_reward:.2f}\n")
                        
                        # clear memory to prevent memory growth (after the reward calculation)
                        for memory in all_memories:
                            memory.clear_memory()

                        # reset all memories
                        del all_memories #https://pytorch.org/docs/stable/multiprocessing.html
                        all_memories = []

                        # Logging every time the model is updated.
                        if loss is not None:

                            if is_sweep: # Wandb for hyperparameter tuning
                                wandb.log({     "iteration": iteration,
                                                "avg_reward": avg_reward, # Set as maximize in the sweep config
                                                "policy_loss": loss['policy_loss'],
                                                "value_loss": loss['value_loss'],
                                                "entropy_loss": loss['entropy_loss'],
                                                "total_loss": loss['total_loss'],
                                                "current_lr_lower": current_lr if train_config['lower_anneal_lr'] else train_config['lr'],
                                                "global_step": global_step          })
                            
                            else: # Tensorboard for regular training
                                
                                total_updates = int(action_timesteps / train_config['lower_update_freq'])
                                writer.add_scalar('Rewards/Average_Reward', avg_reward, global_step)
                                writer.add_scalar('Updates/Total_Policy_Updates', total_updates, global_step)
                                writer.add_scalar('Losses/Policy_Loss', loss['policy_loss'], global_step)
                                writer.add_scalar('Losses/Value_Loss', loss['value_loss'], global_step)
                                writer.add_scalar('Losses/Entropy_Loss', loss['entropy_loss'], global_step)
                                writer.add_scalar('Losses/Total_Loss', loss['total_loss'], global_step)
                                writer.add_scalar('Learning_Rate/Current_LR', current_lr, global_step)
                                print(f"Logged data at step {global_step}")

                                # Save model every n times it has been updated (Important: Not every iteration)
                                if train_config['save_freq'] > 0 and total_updates % train_config['save_freq'] == 0:
                                    torch.save(lower_ppo.policy.state_dict(), os.path.join(save_dir, f'model_iteration_{iteration+1}.pth'))

                                # Save best model so far
                                if avg_reward > best_reward:
                                    best_reward = avg_reward
                                    torch.save(lower_ppo.policy.state_dict(), os.path.join(save_dir, 'best_model.pth'))
                                    
                        else: # For some reason..
                            print("Warning: loss is None")

            except queue.Empty:
                print("Timeout waiting for worker. Continuing...")

        # At the end of an iteration, wait for all processes to finish
        # The join() method is called on each process in the processes list. This ensures that the main program waits for all processes to complete before continuing.
        for p in processes:
            p.join()

        # Store the higher-level transition (TODO: get and add reward for higher level agent)
        # Higher state is of torch geometric Data type.

        higher_memory.append(higher_state, higher_action, higher_logprob, 0, higher_done) # state, action, logprob, reward, done

        # Higher-level agent update. It will be true for the first iteration as well.
        if iteration % train_config['higher_update_freq'] == 0:
            higher_ppo.update(higher_memory, agent_type='higher')

        higher_state = higher_next_state.to(worker_device)

        #     if not is_sweep:
        #         writer.add_scalar('Higher/Reward', higher_reward, global_step)
        #         writer.add_scalar('Higher/Policy_Loss', higher_loss['policy_loss'], global_step)
        #         writer.add_scalar('Higher/Value_Loss', higher_loss['value_loss'], global_step)
        #         writer.add_scalar('Higher/Entropy_Loss', higher_loss['entropy_loss'], global_step)
        #         writer.add_scalar('Higher/Total_Loss', higher_loss['total_loss'], global_step)

    # higher_env.close()

    if is_sweep:
        wandb.finish()
    else:
        writer.close()