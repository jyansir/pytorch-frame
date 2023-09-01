import torch
import torch.nn.functional as F
from torch import Tensor
from torch.nn import GroupNorm, Linear, Parameter

from torch_frame.nn.conv import TableConv


class TromptConv(TableConv):
    r"""The Trompt cell introduced in https://arxiv.org/abs/2305.18446

    Args:
        channels (int): Input/output channel dimensionality
        num_cols (int): Number of columns
        num_prompts (int): Number of prompt columns.
        num_groups (int): Number of groups in group norm. (default: 2)
    """
    def __init__(
        self,
        channels: int,
        num_cols: int,
        num_prompts: int,
        num_groups: int = 2,
    ):
        super().__init__()
        self.channels = channels
        self.num_cols = num_cols
        self.num_prompts = num_prompts

        # Input-independent embeddings for columns and prompts
        self.embedding_column = Parameter(torch.empty(num_cols, channels))
        self.embedding_prompt = Parameter(torch.empty(num_prompts, channels))
        # Dense layer for transforming stacked_e_prompt
        self.lin = Linear(2 * channels, channels)
        # Dense layer for expanding x
        self.weight = Parameter(torch.empty(num_prompts))
        # Group norm
        self.group_norm = GroupNorm(num_groups=num_groups,
                                    num_channels=num_prompts)
        self.reset_parameters()

    def reset_parameters(self):
        torch.nn.init.normal_(self.embedding_column, std=0.1)
        torch.nn.init.normal_(self.embedding_prompt, std=0.1)
        self.lin.reset_parameters()
        torch.nn.init.normal_(self.weight, std=0.1)
        self.group_norm.reset_parameters()

    def forward(self, x: Tensor, x_prompt: Tensor) -> Tensor:
        r"""Transforms :obj:`x` and :obj:`x_prompt` into :obj:`x_prompt` for
        the next layer.

        Args:
            x (Tensor): Feature-based embedding of shape
                [batch_size, num_cols, channels]
            x_prompt (Tensor): Input prompt embeddings of shape
                [batch_size, num_prompts, channels]

        Returns:
            x_prompt (Tensor): Output prompt embeddings for the next layer. The
                shape is [batch_size, num_prompts, channels].
        """
        batch_size = len(x)
        assert x.shape == (batch_size, self.num_cols, self.channels)
        assert x_prompt.shape == (batch_size, self.num_prompts, self.channels)

        # Step1: Compute stacked_e_prompt
        # [batch_size, num_prompts, channels]
        stacked_e_prompt = self.embedding_prompt.repeat(batch_size, 1, 1)
        # [batch_size, num_prompts, 2*channels]
        stacked_e_prompt_cat = torch.cat([stacked_e_prompt, x_prompt], dim=-1)
        # [batch_size, num_prompts, channels]
        stacked_e_prompt = stacked_e_prompt + x_prompt + self.lin(
            stacked_e_prompt_cat)

        # Step2: Compute stacked_e_column
        # [batch_size, num_cols, channels]
        stacked_e_column = self.embedding_column.repeat(batch_size, 1, 1)

        # Step 3: Use stacked_e_prompt and stacked_e_column to compute
        # M_importance
        # [batch_size, num_prompts, channels], [batch_size, num_cols, channels]
        # -> [batch_size, num_prompts, num_cols]
        m_importance = torch.einsum('ijl,ikl->ijk', stacked_e_prompt,
                                    stacked_e_column)
        m_importance = F.softmax(m_importance, dim=-1)
        # [batch_size, num_prompts, num_cols, 1]
        m_importance = m_importance.unsqueeze(dim=-1)

        # Step 4: Expand x ([batch_size, num_cols, channels]) into
        # shape [batch_size, num_prompts, num_cols, channels]
        x = torch.einsum('ijl,k->ikjl', x, self.weight)
        x = F.relu(x)
        x = self.group_norm(x)

        # Step 5: Use x and m_importance to compute x_prompt for the next layer
        # x * m_importance: shape [batch_size, num_prompts, num_cols, channels]
        # x_prompt: shape [batch_size, num_prompts, channels]
        x_prompt = (x * m_importance).sum(dim=2)

        return x_prompt