from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

from web3 import Web3

from pmus.config import Settings

try:
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import DepositWalletCall
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
except ImportError:
    RelayClient = None  # type: ignore[assignment, misc]
    DepositWalletCall = None  # type: ignore[assignment, misc]
    BuilderConfig = None  # type: ignore[assignment, misc]
    BuilderApiKeyCreds = None  # type: ignore[assignment, misc]


CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
COLLATERAL_TOKEN = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

CTF_MERGE_ABI: list[dict[str, Any]] = [
    {
        "inputs": [
            {"internalType": "address", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "partition", "type": "uint256[]"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "mergePositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


class PositionMerger:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self.relay_client: Any | None = None
        self.w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))

        priv_key = getattr(settings, "private_key", None) or getattr(settings, "polygon_private_key", None)
        if (
            RelayClient is not None
            and settings.builder_api_key
            and settings.builder_secret
            and settings.builder_passphrase
            and priv_key
        ):
            try:
                creds = BuilderApiKeyCreds(
                    key=settings.builder_api_key,
                    secret=settings.builder_secret,
                    passphrase=settings.builder_passphrase,
                )
                b_cfg = BuilderConfig(local_builder_creds=creds)
                self.relay_client = RelayClient(
                    relayer_url=settings.relayer_url,
                    chain_id=settings.chain_id,
                    private_key=priv_key,
                    builder_config=b_cfg,
                    rpc_url=settings.polygon_rpc_url,
                )
            except Exception as exc:
                self.logger.warning("Failed to initialize RelayClient: %s", exc)
                self.relay_client = None

    def merge_positions(
        self,
        condition_id: str,
        amount_shares: Decimal,
        wallet_address: str | None = None,
    ) -> tuple[bool, str | None]:
        if not self.settings.enable_auto_merge or amount_shares < self.settings.min_merge_shares:
            return (False, "disabled_or_below_min")

        try:
            cid = condition_id[2:] if condition_id.startswith(("0x", "0X")) else condition_id
            try:
                cond_id_bytes = bytes.fromhex(cid.rjust(64, "0"))
            except ValueError:
                cond_id_bytes = Web3.keccak(text=condition_id)

            ctf_address = Web3.to_checksum_address(CTF_ADDRESS)
            collateral_token = Web3.to_checksum_address(COLLATERAL_TOKEN)
            partition = [1, 2]
            amount_raw = int(amount_shares * Decimal("1000000"))
            parent_collection_id = bytes(32)

            ctf_contract = self.w3.eth.contract(address=ctf_address, abi=CTF_MERGE_ABI)
            calldata = ctf_contract.encode_abi(
                "mergePositions",
                [collateral_token, parent_collection_id, cond_id_bytes, partition, amount_raw],
            )

            target_wallet = (
                wallet_address
                or getattr(self.settings, "address", None)
                or getattr(self.settings, "proxy_wallet_address", None)
                or getattr(self.settings, "polymarket_proxy_address", None)
            )

            # Try Relayer v2 execution first if available
            if self.relay_client is not None and target_wallet and DepositWalletCall is not None:
                try:
                    nonce_resp = self.relay_client.get_nonce(target_wallet, "WALLET")
                    nonce_str = str(
                        nonce_resp.get("nonce", "0")
                        if isinstance(nonce_resp, dict)
                        else getattr(nonce_resp, "nonce", "0")
                    )
                    deadline = str(int(time.time()) + 3600)
                    call = DepositWalletCall(target=ctf_address, value="0", data=calldata)
                    resp = self.relay_client.execute_deposit_wallet_batch(
                        calls=[call],
                        wallet_address=target_wallet,
                        nonce=nonce_str,
                        deadline=deadline,
                    )
                    tx_hash = (
                        getattr(resp, "transaction_hash", None)
                        or getattr(resp, "hash", None)
                        or (
                            resp.get("transactionHash") or resp.get("transaction_hash")
                            if isinstance(resp, dict)
                            else None
                        )
                    )
                    if tx_hash:
                        tx_str = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
                        return (True, tx_str)
                    elif getattr(resp, "transaction_id", None):
                        return (True, str(resp.transaction_id))
                    elif isinstance(resp, dict) and resp.get("transactionID"):
                        return (True, str(resp.get("transactionID")))
                except Exception as relayer_exc:
                    self.logger.warning(
                        "Relayer merge failed (%s), falling back to Web3 direct execution",
                        relayer_exc,
                    )

            # Fallback to direct Web3 execution if private_key is available
            priv_key = getattr(self.settings, "private_key", None) or getattr(self.settings, "polygon_private_key", None)
            if not priv_key:
                return (False, "no_private_key_for_fallback")

            if not priv_key.startswith("0x"):
                priv_key = "0x" + priv_key

            signer_account = self.w3.eth.account.from_key(priv_key)
            nonce = self.w3.eth.get_transaction_count(signer_account.address)
            gas_price = self.w3.eth.gas_price
            tx = {
                "to": ctf_address,
                "from": signer_account.address,
                "data": calldata,
                "nonce": nonce,
                "chainId": self.settings.chain_id,
                "gas": 300000,
                "gasPrice": gas_price,
            }
            try:
                est_gas = self.w3.eth.estimate_gas(tx)
                tx["gas"] = int(est_gas * 1.2)
            except Exception:
                pass

            signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=priv_key)
            raw_tx = getattr(signed_tx, "raw_transaction", None) or getattr(signed_tx, "rawTransaction", None)
            tx_hash_bytes = self.w3.eth.send_raw_transaction(raw_tx)
            tx_hash_hex = tx_hash_bytes.hex() if hasattr(tx_hash_bytes, "hex") else str(tx_hash_bytes)
            return (True, tx_hash_hex)

        except Exception as exc:
            self.logger.error("Auto-merge failed for condition %s: %s", condition_id, exc)
            return (False, str(exc))
