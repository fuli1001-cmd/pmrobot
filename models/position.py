"""Position data models."""

from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import datetime


@dataclass
class TokenBalance:
    """
    Balance of a specific token.
    """
    token_id: str
    balance: float
    last_updated: datetime = field(default_factory=datetime.now)

    @property
    def has_balance(self) -> bool:
        """Check if there's a non-zero balance."""
        return self.balance > 0


@dataclass
class Position:
    """
    Represents a position in a market (holds both Yes and No tokens).
    """
    condition_id: str
    yes_token_id: str
    no_token_id: str
    yes_balance: float = 0.0
    no_balance: float = 0.0
    
    @property
    def can_merge(self) -> bool:
        """Check if position can be merged (has both Yes and No)."""
        return self.yes_balance > 0 and self.no_balance > 0
    
    @property
    def mergeable_amount(self) -> float:
        """Amount that can be merged (minimum of Yes and No)."""
        return min(self.yes_balance, self.no_balance)
    
    @property
    def total_value(self) -> float:
        """
        Total value if merged (1 USDC per pair).
        
        Note: This assumes the position was acquired through arbitrage
        where avg_yes + avg_no < 1.0
        """
        return self.mergeable_amount


@dataclass
class AccountState:
    """
    Overall account state including USDC and all positions.
    """
    usdc_balance: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    last_merge_time: Optional[datetime] = None
    
    @property
    def total_mergeable_value(self) -> float:
        """Total value that can be recovered through merge."""
        return sum(p.mergeable_amount for p in self.positions.values())
    
    def get_position(self, condition_id: str) -> Optional[Position]:
        """Get position for a specific market."""
        return self.positions.get(condition_id)
    
    def update_position(
        self,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        yes_delta: float = 0.0,
        no_delta: float = 0.0,
    ) -> Position:
        """
        Update or create a position.
        
        Args:
            condition_id: Market condition ID
            yes_token_id: Yes token ID
            no_token_id: No token ID
            yes_delta: Change in Yes balance
            no_delta: Change in No balance
            
        Returns:
            Updated position
        """
        if condition_id not in self.positions:
            self.positions[condition_id] = Position(
                condition_id=condition_id,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
            )
        
        pos = self.positions[condition_id]
        pos.yes_balance += yes_delta
        pos.no_balance += no_delta
        
        return pos
    
    def clear_merged_position(self, condition_id: str, merged_amount: float) -> None:
        """
        Update position after a successful merge.
        
        Args:
            condition_id: Market condition ID
            merged_amount: Amount that was merged
        """
        if condition_id in self.positions:
            pos = self.positions[condition_id]
            pos.yes_balance -= merged_amount
            pos.no_balance -= merged_amount
            
            # Update USDC balance
            self.usdc_balance += merged_amount
            self.last_merge_time = datetime.now()
            
            # Remove empty positions
            if pos.yes_balance <= 0 and pos.no_balance <= 0:
                del self.positions[condition_id]
