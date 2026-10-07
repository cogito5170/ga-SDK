"""ga.llm — the one gateway to every model call (R1). See gateway.py."""
from .gateway import (Card, Gateway, GatewayConfig, Result, call, create_runner, default_gateway,  # noqa: F401
                      run_turn)
