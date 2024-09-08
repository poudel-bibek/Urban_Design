import torch
import torch.nn as nn
from torch.distributions import Categorical, Bernoulli
import math

######## MLP model ########
class MLPActorCritic(nn.Module):
    def __init__(self, state_dim, action_dim, device):
        """
        A simple MLP Actor-Critic network 
        Since negative values can sparingly occur, use leaky ReLU.
        # MLP network Param count: ~around 138,000

        Since I expect the output to be binary, I need to apply the sigmoid somewhere. 
        The network needs to understand that the 10 choices are binary. (Done in the act function)

        The input is flat
        """
        super(MLPActorCritic, self).__init__()
        self.device = device
        
        # Shared layers
        self.shared_layers = nn.Sequential(
            nn.Linear(state_dim, 256),
            nn.LeakyReLU(),
            nn.Linear(256, 128),
            nn.LeakyReLU(),
        ).to(device)
        
        # Actor-specific layers
        self.actor_layers = nn.Sequential(
            nn.Linear(128, 64),
            nn.LeakyReLU(),
            nn.Linear(64, action_dim), # Decided not to use sigmoid here but instead later in the act function.
        ).to(device)
        
        # Critic-specific layers
        self.critic_layers = nn.Sequential(
            nn.Linear(128, 64),
            nn.LeakyReLU(),
            nn.Linear(64, 1),  # Corrected from 16 to 64
        ).to(device)
    
    # Actor Network
    def actor(self, state):
        shared_features = self.shared_layers(state)
        return self.actor_layers(shared_features)
    
    # Critic Network
    def critic(self, state):
        shared_features = self.shared_layers(state)
        return self.critic_layers(shared_features)
    
    def act(self, state):
        """
        Select an action based on the current state
        """
        action_logits = self.actor(state)  # outputs logits for each binary decision
        action_probs = torch.sigmoid(action_logits) # convert these logits to probabilities
        dist = Bernoulli(action_probs) # create a Bernoulli distribution using these probabilities
        action = dist.sample() # sample from this distribution to get our binary actions.
        return action.long(), dist.log_prob(action).sum(-1) #  return the actions and the sum of their log probabilities (sum along the last dimension)
        # TODO: check the validity of the sum operation. 

    def evaluate(self, states, actions):
        action_logits = self.actor(states)
        action_probs = torch.sigmoid(action_logits)
        dist = Bernoulli(action_probs)
        
        action_logprobs = dist.log_prob(actions.float())
        dist_entropy = dist.entropy()
        state_values = self.critic(states)
        
        return action_logprobs.sum(-1), state_values, dist_entropy

    def param_count(self, ):
        """
        Return a dict
        """
        actor_params = sum(p.numel() for p in self.actor_layers.parameters())
        critic_params = sum(p.numel() for p in self.critic_layers.parameters())
        shared_params = sum(p.numel() for p in self.shared_layers.parameters())
        
        return {
            "actor_total": actor_params + shared_params,
            "critic_total": critic_params + shared_params,
            "total": actor_params + critic_params + shared_params
        }

######## CNN model ########
class CNNActorCritic(nn.Module):
    def __init__(self, in_channels, action_dim, device, size='medium', kernel_size=3):
        """
        CNN Actor-Critic network with configurable size (designed to be compatible with hyper-parameter tuning)
        we are applying conv2d, the state should be 2d with a bunch of channels.
        Choices: 
            size: 'small' or 'medium' (default)
            kernel_size: 3 (default) or 5
        """
        super(CNNActorCritic, self).__init__()
        self.device = device
        self.in_channels = in_channels
        
        padding = kernel_size // 2  # Ensures output size remains the same
        
        if size == 'small':
            self.shared_cnn = nn.Sequential(
                nn.Conv2d(in_channels, 16, kernel_size=kernel_size, stride=1, padding=padding),
                nn.LeakyReLU(),
                nn.Conv2d(16, 32, kernel_size=kernel_size, stride=1, padding=padding),
                nn.LeakyReLU(),
                nn.Flatten()
            ).to(device)
            
            hidden_dim = 128

        else:  # medium
            self.shared_cnn = nn.Sequential(
                nn.Conv2d(in_channels, 32, kernel_size=kernel_size, stride=1, padding=padding),
                nn.LeakyReLU(),
                nn.Conv2d(32, 64, kernel_size=kernel_size, stride=1, padding=padding),
                nn.LeakyReLU(),
                nn.Conv2d(64, 64, kernel_size=kernel_size, stride=1, padding=padding),
                nn.LeakyReLU(),
                nn.Flatten()
            ).to(device)
            
            hidden_dim = 256
        
        # Calculate the size of the flattened CNN output
        with torch.no_grad():
            sample_input = torch.zeros(1, in_channels).to(device) 
            cnn_output_size = self.shared_cnn(sample_input).shape[1]
            print(f"\n\nCNN output size: {cnn_output_size}\n\n")

        # Actor-specific layers
        self.actor_layers = nn.Sequential(
            nn.Linear(cnn_output_size, hidden_dim),
            nn.LeakyReLU(),
            nn.Linear(hidden_dim, action_dim)
        ).to(device)
        
        # Critic-specific layers
        self.critic_layers = nn.Sequential(
            nn.Linear(cnn_output_size, hidden_dim),
            nn.LeakyReLU(),
            nn.Linear(hidden_dim, 1)
        ).to(device)
    
    def actor(self, state):
        shared_features = self.shared_cnn(state)
        return self.actor_layers(shared_features)
    
    def critic(self, state):
        shared_features = self.shared_cnn(state)
        return self.critic_layers(shared_features)
    
    def act(self, state):
        """
        Select an action based on the current state
        """
        print(f"\n\nState shape: {state.shape}\n\n")
        
        state = state.view(1, self.in_channels)
        action_logits = self.actor(state)
        action_probs = torch.sigmoid(action_logits)
        dist = Bernoulli(action_probs)
        action = dist.sample()
        return action.long(), dist.log_prob(action).sum(-1)

    def evaluate(self, states, actions):
        action_logits = self.actor(states)
        action_probs = torch.sigmoid(action_logits)
        dist = Bernoulli(action_probs)
        
        action_logprobs = dist.log_prob(actions.float())
        dist_entropy = dist.entropy()
        state_values = self.critic(states)
        
        return action_logprobs.sum(-1), state_values, dist_entropy

    def param_count(self, ):
        """
        Return a dict
        """
        actor_params = sum(p.numel() for p in self.actor_layers.parameters())
        critic_params = sum(p.numel() for p in self.critic_layers.parameters())
        shared_params = sum(p.numel() for p in self.shared_cnn.parameters())
        
        return {
            "actor_total": actor_params + shared_params,
            "critic_total": critic_params + shared_params,
            "total": actor_params + critic_params + shared_params
        }

######## Transformer model ########
