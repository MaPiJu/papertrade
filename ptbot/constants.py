"""PaperTrade protocol constants.

PaperTrade publishes no official SDK or ABI. These values come from community
reverse-engineering of the exchange.papertrade.xyz frontend (October 10, 2026)
and from on-chain reads. The bot re-checks the EIP-712 domains on-chain at
startup (EIP-5267) and the minimum sizes via /state/trading.
"""

CHAIN_ID = 999  # HyperEVM mainnet

EXCHANGE_BASE = "https://exchange.papertrade.xyz"
HYPEREVM_RPC = "https://rpc.hyperliquid.xyz/evm"
HL_API = "https://api.hyperliquid.xyz"

EXCHANGE_CONTRACT = "0x6cd5661646289fb6e65ea5c032310fded797d0a2"
PAPER_TOKEN = "0xe40f17915daa230324030003a197cdaef2261c0e"
PAPER_STAKING = "0xaad6c7b0cc3014fc80ffedae0ed7ce5967b0f016"
PAPER_TOKENOMICS = "0x75174549dbbd44497d31a47e2e2d7406408d63d9"

# USDC token id on Hyperliquid spot (mainnet), used by spotSend.
HL_USDC_TOKEN = "USDC:0x6d1e7cde53ba9467b783cb7c530ce054"

INSTRUMENTS = {"BTC": 0, "ETH": 1}

# Every amount on the exchange (margin, balances, PAPER) uses 18 decimals.
SCALE = 10**18

# Liquidation fires ~5 bps before equity hits zero: distance = 1/leverage - 0.05%.
LIQ_BUFFER = 0.0005

# Fallbacks used when /state/trading cannot be read.
DEFAULT_MIN_MARGIN_USD = 10.0
DEFAULT_MIN_NOTIONAL_USD = 10_000.0

# Lifetimes of signed intents: after the deadline the relayer can no longer
# execute them, which is what lets the bot safely retry.
STAKE_DEADLINE_S = 300
WITHDRAW_DEADLINE_S = 600

FEE_ADDRESS = "0x0000000000000000000000000000000000000000"
SESSION_KEY_LIFETIME_S = 30 * 24 * 3600

DOMAIN_EXCHANGE = {"name": "String", "version": "1", "chainId": CHAIN_ID,
                   "verifyingContract": EXCHANGE_CONTRACT}
DOMAIN_STAKING = {"name": "PaperStaking", "version": "1", "chainId": CHAIN_ID,
                  "verifyingContract": PAPER_STAKING}

INTENT_TYPES = {
    "RegisterSessionKey": [
        {"name": "user", "type": "address"},
        {"name": "key", "type": "address"},
        {"name": "expiry", "type": "uint64"},
        {"name": "feeAddress", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ],
    "OpenPosition": [
        {"name": "user", "type": "address"},
        {"name": "instrumentId", "type": "uint32"},
        {"name": "isLong", "type": "bool"},
        {"name": "size", "type": "uint128"},
        {"name": "leverage", "type": "uint32"},
        {"name": "useDebt", "type": "bool"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ],
    "Close": [
        {"name": "user", "type": "address"},
        {"name": "positionIds", "type": "uint256[]"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ],
    "WithdrawToCore": [
        {"name": "user", "type": "address"},
        {"name": "amount", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ],
    "Stake": [
        {"name": "user", "type": "address"},
        {"name": "amount", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ],
    "Claim": [
        {"name": "user", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ],
}

# Which domain signs which intent, and the action name POSTed to /intents.
INTENT_DOMAIN_KEY = {
    "RegisterSessionKey": "exchange",
    "OpenPosition": "exchange",
    "Close": "exchange",
    "WithdrawToCore": "exchange",
    "Stake": "staking",
    "Claim": "staking",
}
INTENT_ACTION = {
    "RegisterSessionKey": "registerSessionKey",
    "OpenPosition": "openPosition",
    "Close": "closePositions",
    "WithdrawToCore": "withdrawToCore",
    "Stake": "paperStake",
    "Claim": "paperClaim",
}
