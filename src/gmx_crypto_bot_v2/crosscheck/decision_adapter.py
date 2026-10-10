"""Fail-closed same-order execution-decision evidence gate.

This adapter names every currently identified MarketIncrease rule. It does not
mistake a narrow acceptable-price calculation for the full GMX decision.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gmx_crypto_bot_v2.domain.keys import keccak256
from gmx_crypto_bot_v2.domain.configuration import ConfigKey
from gmx_crypto_bot_v2.domain.keys import config_market_data
from gmx_crypto_bot_v2.domain.swap_keys import key as storage_key


MARKET_INCREASE_RULES: tuple[tuple[str, str], ...] = (
    ("pending_latest_request", "router_preflight"),
    ("order_field_identity", "pinned_sidecar"),
    ("execute_order_feature_enabled", "pinned_sidecar"),
    ("oracle_price_source_and_age", "recorded_oracle_evidence"),
    ("order_valid_from_and_expiration", "request_expiration_config"),
    ("market_and_collateral_token_valid", "market_config"),
    ("swap_path_and_min_output", "swap_state"),
    ("execution_price_and_acceptable_price", "independent_economics"),
    ("position_fees_and_collateral_sufficiency", "independent_economics"),
    ("max_open_interest", "max_open_interest_config"),
    ("reserve_and_open_interest_reserve", "reserve_config"),
    ("minimum_position_and_collateral", "position_risk_config"),
    ("post_update_position_validity", "position_risk_config"),
    ("market_token_balances", "market_balance_state"),
    ("gas_and_keeper_checks", "execution_context"),
)


def _digest(path: Path) -> str:
    return "0x" + hashlib.sha256(path.read_bytes()).hexdigest()


def verify_pinned_decision_config(config_path: Path, source_path: Path,
                                  sidecar: dict[str, Any]) -> dict[str, Any]:
    """Verify the two historical DataStore reads and their exact-match source keys."""
    config = json.loads(config_path.read_text())
    source = json.loads(source_path.read_text())
    pin, deployment = sidecar["pin"], sidecar["deployment"]
    market = sidecar["pinned_order"]["market"]
    if config.get("schema") != "GmxStep41PinnedDecisionConfig" or config.get("version") != 1 or \
            config.get("chain_id") != deployment["chain_id"] or \
            config.get("block_number") != pin["number"] or \
            config.get("block_hash", "").lower() != pin["recorded_hash"].lower() or \
            config.get("datastore", "").lower() != deployment["datastore"].lower() or \
            config.get("market", "").lower() != market.lower():
        raise ValueError("pinned decision config identity mismatch")
    if source.get("runtimeMatch") != "exact_match" or source.get("match") != "exact_match" or \
            source.get("compilation", {}).get("fullyQualifiedName") != \
            "contracts/order/IncreaseOrderExecutor.sol:IncreaseOrderExecutor":
        raise ValueError("pinned decision config source identity mismatch")
    files = source.get("sources", {})
    expected = {
        "contracts/data/Keys.sol": (
            'REQUEST_EXPIRATION_TIME = keccak256(abi.encode("REQUEST_EXPIRATION_TIME"))',
            'IS_MARKET_DISABLED = keccak256(abi.encode("IS_MARKET_DISABLED"))',
            "function isMarketDisabledKey(address market)", "IS_MARKET_DISABLED,\n            market"),
        "contracts/order/IncreaseOrderUtils.sol": (
            "params.minOracleTimestamp < params.order.updatedAtTime()",
            "!Order.isMarketOrder(params.order.orderType()) &&\n"
            "            params.minOracleTimestamp < params.order.validFromTime()",
            "params.maxOracleTimestamp > params.order.updatedAtTime() + requestExpirationTime",
            "MarketUtils.validatePositionMarket"),
        "contracts/order/Order.sol": (
            "function isMarketOrder(OrderType _orderType)",
            "_orderType == OrderType.MarketIncrease",),
        "contracts/market/MarketUtils.sol": (
            "validateEnabledMarket(dataStore, market)",
            "dataStore.getBoolValueFromDataStore(Keys.isMarketDisabledKey(market.marketToken))",),
        "contracts/data/DataStore.sol": ("function getUint(bytes32 key)",
                                         "function getBool(bytes32 key)"),
    }
    if any(path not in files or any(fragment not in files[path]["content"]
                                    for fragment in fragments)
           for path, fragments in expected.items()):
        raise ValueError("pinned decision config historical source mismatch")
    keys = {
        "request_expiration_time": ConfigKey("REQUEST_EXPIRATION_TIME").storage_key,
        "is_market_disabled": ConfigKey("IS_MARKET_DISABLED", config_market_data(market)).storage_key,
    }
    if config.get("keys") != keys:
        raise ValueError("pinned decision config storage key mismatch")
    transcript = config.get("rpc_transcript")
    if not isinstance(transcript, list) or any(not isinstance(row, dict) for row in transcript) or \
            [row.get("method") for row in transcript] != [
            "eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_call",
            "eth_getBlockByNumber"]:
        raise ValueError("pinned decision config transcript shape mismatch")
    block_tag = hex(pin["number"])
    try:
        if transcript[0].get("params") != [] or \
                int(transcript[0]["response"]["result"], 16) != deployment["chain_id"] or \
                any(transcript[i].get("params") != [block_tag, False] or
                    int(transcript[i]["response"]["result"]["number"], 16) != pin["number"] or
                    transcript[i]["response"]["result"]["hash"].lower() != pin["recorded_hash"].lower()
                    for i in (1, 4)):
            raise ValueError("pinned decision config block transcript mismatch")
        values = {}
        for index, (name, signature) in enumerate((
                ("request_expiration_time", "getUint(bytes32)"),
                ("is_market_disabled", "getBool(bytes32)")), start=2):
            row = transcript[index]
            expected_call = [{"to": deployment["datastore"],
                              "data": "0x" + keccak256(signature.encode())[:4].hex() + keys[name][2:]},
                             block_tag]
            params = row.get("params")
            if not isinstance(params, list) or len(params) != 2 or \
                    not isinstance(params[0], dict) or \
                    {**params[0], "to": params[0].get("to", "").lower()} != \
                    {**expected_call[0], "to": expected_call[0]["to"].lower()} or \
                    params[1] != expected_call[1]:
                raise ValueError("pinned decision config calldata mismatch")
            raw = row["response"]["result"]
            if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x"):
                raise ValueError("pinned decision config ABI result mismatch")
            values[name] = int(raw, 16)
        if values["is_market_disabled"] not in (0, 1) or \
                config.get("values") != {"request_expiration_time": values["request_expiration_time"],
                                         "is_market_disabled": bool(values["is_market_disabled"])}:
            raise ValueError("pinned decision config decoded value mismatch")
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("pinned decision config malformed transcript") from error
    return config


def verify_pinned_risk_cells(path: Path, source_path: Path,
                             sidecar: dict[str, Any]) -> dict[str, Any]:
    """Read independently keyed uints from the selected block transcript."""
    record = json.loads(path.read_text())
    source = json.loads(source_path.read_text())
    pin, deployment = sidecar["pin"], sidecar["deployment"]
    market = sidecar["fixed_values"]["market"]
    market_token = market["MARKET_TOKEN"]
    if record.get("schema") != "GmxStep41PinnedRiskCells" or record.get("version") != 1 or \
            record.get("chain_id") != deployment["chain_id"] or \
            record.get("block_number") != pin["number"] or \
            record.get("block_hash", "").lower() != pin["recorded_hash"].lower() or \
            record.get("datastore", "").lower() != deployment["datastore"].lower():
        raise ValueError("pinned risk cells identity mismatch")
    files = source.get("sources", {})
    source_fragments = {
        "contracts/data/Keys.sol": (
            'MIN_POSITION_SIZE_USD = keccak256(abi.encode("MIN_POSITION_SIZE_USD"))',
            'MIN_COLLATERAL_USD = keccak256(abi.encode("MIN_COLLATERAL_USD"))',
            'MIN_COLLATERAL_FACTOR = keccak256(abi.encode("MIN_COLLATERAL_FACTOR"))',
            'MIN_COLLATERAL_FACTOR_FOR_OPEN_INTEREST_MULTIPLIER = keccak256(abi.encode("MIN_COLLATERAL_FACTOR_FOR_OPEN_INTEREST_MULTIPLIER"))',
            'RESERVE_FACTOR = keccak256(abi.encode("RESERVE_FACTOR"))',
            'OPEN_INTEREST_RESERVE_FACTOR = keccak256(abi.encode("OPEN_INTEREST_RESERVE_FACTOR"))',
            'OPEN_INTEREST = keccak256(abi.encode("OPEN_INTEREST"))',
            'MAX_OPEN_INTEREST = keccak256(abi.encode("MAX_OPEN_INTEREST"))',
            "function reserveFactorKey(address market, bool isLong)",
            "function openInterestReserveFactorKey(address market, bool isLong)",
            "function openInterestKey(address market, address collateralToken, bool isLong)",
            "function maxOpenInterestKey(address market, bool isLong)",
            "function minCollateralFactorKey(address market)",
            "function minCollateralFactorForOpenInterestMultiplierKey(address market, bool isLong)"),
        "contracts/position/PositionUtils.sol": (
            "uint256 minPositionSizeUsd = dataStore.getUint(Keys.MIN_POSITION_SIZE_USD)",
            "if (position.sizeInUsd() < minPositionSizeUsd)",),
        "contracts/market/MarketUtils.sol": (
            "function validateReserve(", "function validateOpenInterestReserve(",
            "function validateOpenInterest(", "getMaxOpenInterest(dataStore, market.marketToken, isLong)",
            "uint256 reservedUsd = getReservedUsd(",
            "reservedUsd = openInterestInTokens * prices.indexTokenPrice.max",
            "uint256 maxReservedUsd = Precision.applyFactor(poolUsd, reserveFactor)",
            "if (reservedUsd > maxReservedUsd)",
            "return poolAmount * tokenPrice"),
    }
    if any(file not in files or any(fragment not in files[file]["content"]
                                    for fragment in fragments)
           for file, fragments in source_fragments.items()):
        raise ValueError("pinned risk cells historical source mismatch")
    keys = {
        "min_position_size_usd": storage_key("MIN_POSITION_SIZE_USD"),
        "reserve_factor_long": storage_key("RESERVE_FACTOR", market_token, True),
        "open_interest_reserve_factor_long": storage_key(
            "OPEN_INTEREST_RESERVE_FACTOR", market_token, True),
        "long_oi_usd_weth_collateral": storage_key(
            "OPEN_INTEREST", market_token, market["LONG_TOKEN"], True),
        "long_oi_usd_usdc_collateral": storage_key(
            "OPEN_INTEREST", market_token, market["SHORT_TOKEN"], True),
        "min_collateral_usd": storage_key("MIN_COLLATERAL_USD"),
        "min_collateral_factor": storage_key("MIN_COLLATERAL_FACTOR", market_token),
        "min_collateral_factor_for_oi_multiplier_long": storage_key(
            "MIN_COLLATERAL_FACTOR_FOR_OPEN_INTEREST_MULTIPLIER", market_token, True),
        "max_open_interest_long": storage_key("MAX_OPEN_INTEREST", market_token, True),
    }
    if record.get("keys") != keys:
        raise ValueError("pinned risk cells storage key mismatch")
    transcript = record.get("rpc_transcript")
    if not isinstance(transcript, list) or any(not isinstance(row, dict) for row in transcript) or \
            len(transcript) < len(keys) + 3 or \
            [row.get("method") for row in transcript[:2]] != ["eth_chainId", "eth_getBlockByNumber"] or \
            transcript[-1].get("method") != "eth_getBlockByNumber" or \
            sum(row.get("method") == "eth_call" for row in transcript) != len(keys) or \
            any(row.get("method") not in {"eth_chainId", "eth_getBlockByNumber", "eth_call"}
                for row in transcript) or \
            sum(row.get("method") == "eth_chainId" for row in transcript) != 1:
        raise ValueError("pinned risk cells transcript shape mismatch")
    block_tag = hex(pin["number"])
    try:
        if transcript[0].get("params") != [] or \
                int(transcript[0]["response"]["result"], 16) != deployment["chain_id"] or \
                any(row.get("params") != [block_tag, False] or
                    int(row["response"]["result"]["number"], 16) != pin["number"] or
                    row["response"]["result"]["hash"].lower() != pin["recorded_hash"].lower()
                    for row in transcript if row.get("method") == "eth_getBlockByNumber"):
            raise ValueError("pinned risk cells block transcript mismatch")
        values = {}
        selector = "0x" + keccak256(b"getUint(bytes32)")[:4].hex()
        calls = [row for row in transcript if row.get("method") == "eth_call"]
        for row, (name, value_key) in zip(calls, keys.items()):
            params = row.get("params")
            if not isinstance(params, list) or len(params) != 2 or \
                    not isinstance(params[0], dict) or \
                    {**params[0], "to": params[0].get("to", "").lower()} != \
                    {"to": deployment["datastore"].lower(), "data": selector + value_key[2:]} or \
                    params[1] != block_tag:
                raise ValueError("pinned risk cells calldata mismatch")
            raw = row["response"]["result"]
            if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x"):
                raise ValueError("pinned risk cells ABI result mismatch")
            values[name] = int(raw, 16)
        if record.get("values") != values:
            raise ValueError("pinned risk cells decoded value mismatch")
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("pinned risk cells malformed transcript") from error
    return record


def verify_pinned_balance_inputs(path: Path, source_path: Path,
                                  sidecar: dict[str, Any]) -> dict[str, Any]:
    """Bind the historical OI-balance branch selector and short USD OI cells."""
    record = json.loads(path.read_text())
    source = json.loads(source_path.read_text())
    pin, deployment = sidecar["pin"], sidecar["deployment"]
    market = sidecar["fixed_values"]["market"]
    if record.get("schema") != "GmxStep41PinnedBalanceInputs" or record.get("version") != 1 or \
            record.get("chain_id") != deployment["chain_id"] or \
            record.get("block_number") != pin["number"] or \
            record.get("block_hash", "").lower() != pin["recorded_hash"].lower() or \
            record.get("datastore", "").lower() != deployment["datastore"].lower():
        raise ValueError("pinned balance inputs identity mismatch")
    files = source.get("sources", {})
    fragments = {
        "contracts/data/Keys.sol": (
            'USE_OPEN_INTEREST_IN_TOKENS_FOR_BALANCE = keccak256(abi.encode("USE_OPEN_INTEREST_IN_TOKENS_FOR_BALANCE"))',
            "function openInterestKey(address market, address collateralToken, bool isLong)"),
        "contracts/pricing/PositionPricingUtils.sol": (
            "getBool(Keys.USE_OPEN_INTEREST_IN_TOKENS_FOR_BALANCE)",
            "if (useOpenInterestInTokens)",
            "if (priceImpactUsd >= 0) { return (priceImpactUsd, balanceWasImproved); }",
            "params.usdDelta = params.tokenDelta * params.indexTokenPrice.midPrice().toInt256()"),
        "contracts/pricing/PricingUtils.sol": (
            "bool balanceWasImproved = nextDiffUsd < initialDiffUsd",
            "Calc.toSigned(deltaDiffUsd, balanceWasImproved)"),
        "contracts/position/PositionUtils.sol": (
            "cache.baseSizeDeltaInTokens = params.order.sizeDeltaUsd() / prices.indexTokenPrice.max",
            "cache.priceImpactUsd = MarketUtils.capPositiveImpactUsdByMaxPositionImpact",
            "cache.priceImpactAmount = cache.priceImpactUsd / prices.indexTokenPrice.max.toInt256()",
            "sizeDeltaInTokens = cache.baseSizeDeltaInTokens.toInt256() + cache.priceImpactAmount"),
        "contracts/order/BaseOrderUtils.sol": (
            "uint256 executionPrice = sizeDeltaUsd / sizeDeltaInTokens",
            "isLong && executionPrice <= acceptablePrice"),
    }
    if any(file not in files or any(fragment not in files[file]["content"]
                                    for fragment in parts) for file, parts in fragments.items()):
        raise ValueError("pinned balance inputs historical source mismatch")
    keys = {
        "use_oi_tokens_for_balance": storage_key("USE_OPEN_INTEREST_IN_TOKENS_FOR_BALANCE"),
        "short_oi_usd_weth_collateral": storage_key(
            "OPEN_INTEREST", market["MARKET_TOKEN"], market["LONG_TOKEN"], False),
        "short_oi_usd_usdc_collateral": storage_key(
            "OPEN_INTEREST", market["MARKET_TOKEN"], market["SHORT_TOKEN"], False),
    }
    if record.get("keys") != keys:
        raise ValueError("pinned balance inputs storage key mismatch")
    transcript = record.get("rpc_transcript")
    if not isinstance(transcript, list) or any(not isinstance(row, dict) for row in transcript) or \
            [row.get("method") for row in transcript] != [
                "eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_call", "eth_call",
                "eth_getBlockByNumber"]:
        raise ValueError("pinned balance inputs transcript shape mismatch")
    block_tag = hex(pin["number"])
    try:
        if transcript[0].get("params") != [] or \
                int(transcript[0]["response"]["result"], 16) != deployment["chain_id"] or \
                any(transcript[i].get("params") != [block_tag, False] or
                    int(transcript[i]["response"]["result"]["number"], 16) != pin["number"] or
                    transcript[i]["response"]["result"]["hash"].lower() != pin["recorded_hash"].lower()
                    for i in (1, 5)):
            raise ValueError("pinned balance inputs block transcript mismatch")
        values = {}
        for index, (name, value_key) in enumerate(keys.items(), start=2):
            row = transcript[index]
            signature = "getBool(bytes32)" if index == 2 else "getUint(bytes32)"
            params = row.get("params")
            if not isinstance(params, list) or len(params) != 2 or \
                    not isinstance(params[0], dict) or \
                    {**params[0], "to": params[0].get("to", "").lower()} != \
                    {"to": deployment["datastore"].lower(),
                     "data": "0x" + keccak256(signature.encode())[:4].hex() + value_key[2:]} or \
                    params[1] != block_tag:
                raise ValueError("pinned balance inputs calldata mismatch")
            raw = row["response"]["result"]
            if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x"):
                raise ValueError("pinned balance inputs ABI result mismatch")
            decoded = int(raw, 16)
            if index == 2 and decoded not in (0, 1):
                raise ValueError("pinned balance inputs bool result mismatch")
            values[name] = bool(decoded) if index == 2 else decoded
        if record.get("values") != values:
            raise ValueError("pinned balance inputs decoded value mismatch")
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("pinned balance inputs malformed transcript") from error
    return record


def verify_pinned_fee_clocks(path: Path, source_path: Path,
                              sidecar: dict[str, Any]) -> dict[str, Any]:
    """Verify borrowing and funding update clocks at the selected block."""
    record = json.loads(path.read_text())
    source = json.loads(source_path.read_text())
    pin, deployment = sidecar["pin"], sidecar["deployment"]
    market = sidecar["pinned_order"]["market"]
    if record.get("schema") != "GmxStep41PinnedFeeClocks" or record.get("version") != 1 or \
            record.get("chain_id") != deployment["chain_id"] or \
            record.get("block_number") != pin["number"] or \
            record.get("block_hash", "").lower() != pin["recorded_hash"].lower() or \
            record.get("datastore", "").lower() != deployment["datastore"].lower():
        raise ValueError("pinned fee clocks identity mismatch")
    files = source.get("sources", {})
    fragments = {
        "contracts/data/Keys.sol": (
            "function fundingUpdatedAtKey(address market)",
            "function cumulativeBorrowingFactorUpdatedAtKey(address market, bool isLong)"),
        "contracts/market/MarketUtils.sol": (
            "getSecondsSinceFundingUpdated(dataStore, market.marketToken)",
            "getSecondsSinceCumulativeBorrowingFactorUpdated(dataStore, market.marketToken, isLong)",),
        "contracts/position/PositionUtils.sol": ("MarketUtils.updateFundingState(",
                                                 "MarketUtils.updateCumulativeBorrowingFactor("),
    }
    if any(file not in files or any(fragment not in files[file]["content"]
                                    for fragment in parts) for file, parts in fragments.items()):
        raise ValueError("pinned fee clocks historical source mismatch")
    keys = {
        "funding_updated_at": storage_key("FUNDING_UPDATED_AT", market),
        "borrowing_updated_at_long": storage_key(
            "CUMULATIVE_BORROWING_FACTOR_UPDATED_AT", market, True),
    }
    if record.get("keys") != keys:
        raise ValueError("pinned fee clocks storage key mismatch")
    transcript = record.get("rpc_transcript")
    if not isinstance(transcript, list) or any(not isinstance(row, dict) for row in transcript) or \
            [row.get("method") for row in transcript] != [
                "eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_call",
                "eth_getBlockByNumber"]:
        raise ValueError("pinned fee clocks transcript shape mismatch")
    block_tag = hex(pin["number"])
    try:
        if transcript[0].get("params") != [] or \
                int(transcript[0]["response"]["result"], 16) != deployment["chain_id"] or \
                any(transcript[i].get("params") != [block_tag, False] or
                    int(transcript[i]["response"]["result"]["number"], 16) != pin["number"] or
                    transcript[i]["response"]["result"]["hash"].lower() != pin["recorded_hash"].lower() or
                    int(transcript[i]["response"]["result"]["timestamp"], 16) != record.get("block_timestamp")
                    for i in (1, 4)):
            raise ValueError("pinned fee clocks block transcript mismatch")
        values = {}
        selector = "0x" + keccak256(b"getUint(bytes32)")[:4].hex()
        for index, (name, value_key) in enumerate(keys.items(), start=2):
            row = transcript[index]
            params = row.get("params")
            if not isinstance(params, list) or len(params) != 2 or \
                    not isinstance(params[0], dict) or \
                    {**params[0], "to": params[0].get("to", "").lower()} != \
                    {"to": deployment["datastore"].lower(), "data": selector + value_key[2:]} or \
                    params[1] != block_tag:
                raise ValueError("pinned fee clocks calldata mismatch")
            raw = row["response"]["result"]
            if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x"):
                raise ValueError("pinned fee clocks ABI result mismatch")
            values[name] = int(raw, 16)
        if record.get("values") != values or \
                any(value > record["block_timestamp"] for value in values.values()):
            raise ValueError("pinned fee clocks decoded value mismatch")
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("pinned fee clocks malformed transcript") from error
    return record


def verify_pinned_borrowing_skip(path: Path, source_path: Path,
                                  sidecar: dict[str, Any]) -> dict[str, Any]:
    """Verify the smaller-side borrowing switch from a pinned bool call."""
    record = json.loads(path.read_text())
    source = json.loads(source_path.read_text())
    pin, deployment = sidecar["pin"], sidecar["deployment"]
    if record.get("schema") != "GmxStep41PinnedBorrowingSkip" or record.get("version") != 1 or \
            record.get("chain_id") != deployment["chain_id"] or \
            record.get("block_number") != pin["number"] or \
            record.get("block_hash", "").lower() != pin["recorded_hash"].lower() or \
            record.get("datastore", "").lower() != deployment["datastore"].lower() or \
            record.get("key") != storage_key("SKIP_BORROWING_FEE_FOR_SMALLER_SIDE"):
        raise ValueError("pinned borrowing skip identity or key mismatch")
    files = source.get("sources", {})
    fragments = {
        "contracts/data/Keys.sol": (
            'SKIP_BORROWING_FEE_FOR_SMALLER_SIDE = keccak256(abi.encode("SKIP_BORROWING_FEE_FOR_SMALLER_SIDE"))',),
        "contracts/market/MarketUtils.sol": (
            "getBoolValueFromDataStore(Keys.SKIP_BORROWING_FEE_FOR_SMALLER_SIDE)",
            "if (isLong && longOpenInterest < shortOpenInterest)",
            "uint256 delta = durationInSeconds * borrowingFactorPerSecond",
            "uint256 diffFactor = cumulativeBorrowingFactor - position.borrowingFactor()",
            "return 0;"),
    }
    if any(file not in files or any(fragment not in files[file]["content"]
                                    for fragment in parts) for file, parts in fragments.items()):
        raise ValueError("pinned borrowing skip historical source mismatch")
    transcript = record.get("rpc_transcript")
    if not isinstance(transcript, list) or any(not isinstance(row, dict) for row in transcript) or \
            [row.get("method") for row in transcript] != [
                "eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_getBlockByNumber"]:
        raise ValueError("pinned borrowing skip transcript shape mismatch")
    block_tag = hex(pin["number"])
    try:
        if transcript[0].get("params") != [] or \
                int(transcript[0]["response"]["result"], 16) != deployment["chain_id"] or \
                any(transcript[i].get("params") != [block_tag, False] or
                    int(transcript[i]["response"]["result"]["number"], 16) != pin["number"] or
                    transcript[i]["response"]["result"]["hash"].lower() != pin["recorded_hash"].lower()
                    for i in (1, 3)):
            raise ValueError("pinned borrowing skip block transcript mismatch")
        params = transcript[2].get("params")
        expected = {"to": deployment["datastore"].lower(),
                    "data": "0x" + keccak256(b"getBool(bytes32)")[:4].hex() + record["key"][2:]}
        if not isinstance(params, list) or len(params) != 2 or \
                not isinstance(params[0], dict) or \
                {**params[0], "to": params[0].get("to", "").lower()} != expected or \
                params[1] != block_tag:
            raise ValueError("pinned borrowing skip calldata mismatch")
        raw = transcript[2]["response"]["result"]
        if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x"):
            raise ValueError("pinned borrowing skip ABI result mismatch")
        decoded = int(raw, 16)
        if decoded not in (0, 1) or record.get("skip_borrowing_fee_for_smaller_side") is not bool(decoded):
            raise ValueError("pinned borrowing skip decoded value mismatch")
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("pinned borrowing skip malformed transcript") from error
    return record


def verify_pinned_funding_selector(path: Path, source_path: Path,
                                    sidecar: dict[str, Any]) -> dict[str, Any]:
    """Verify the adaptive versus static funding branch selector."""
    record = json.loads(path.read_text())
    source = json.loads(source_path.read_text())
    pin, deployment = sidecar["pin"], sidecar["deployment"]
    market = sidecar["pinned_order"]["market"]
    if record.get("schema") != "GmxStep41PinnedFundingSelector" or record.get("version") != 1 or \
            record.get("chain_id") != deployment["chain_id"] or \
            record.get("block_number") != pin["number"] or \
            record.get("block_hash", "").lower() != pin["recorded_hash"].lower() or \
            record.get("datastore", "").lower() != deployment["datastore"].lower() or \
            record.get("key") != storage_key("FUNDING_INCREASE_FACTOR_PER_SECOND", market):
        raise ValueError("pinned funding selector identity or key mismatch")
    files = source.get("sources", {})
    fragments = {
        "contracts/data/Keys.sol": ("function fundingIncreaseFactorPerSecondKey(address market)",),
        "contracts/market/MarketUtils.sol": (
            "Keys.fundingIncreaseFactorPerSecondKey(market.marketToken)",
            "if (configCache.fundingIncreaseFactorPerSecond == 0)",
            "cache.savedFundingFactorPerSecond = getSavedFundingFactorPerSecond"),
    }
    if any(file not in files or any(fragment not in files[file]["content"]
                                    for fragment in parts) for file, parts in fragments.items()):
        raise ValueError("pinned funding selector historical source mismatch")
    transcript = record.get("rpc_transcript")
    if not isinstance(transcript, list) or any(not isinstance(row, dict) for row in transcript) or \
            [row.get("method") for row in transcript] != [
                "eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_getBlockByNumber"]:
        raise ValueError("pinned funding selector transcript shape mismatch")
    block_tag = hex(pin["number"])
    try:
        if transcript[0].get("params") != [] or \
                int(transcript[0]["response"]["result"], 16) != deployment["chain_id"] or \
                any(transcript[i].get("params") != [block_tag, False] or
                    int(transcript[i]["response"]["result"]["number"], 16) != pin["number"] or
                    transcript[i]["response"]["result"]["hash"].lower() != pin["recorded_hash"].lower()
                    for i in (1, 3)):
            raise ValueError("pinned funding selector block transcript mismatch")
        params = transcript[2].get("params")
        expected = {"to": deployment["datastore"].lower(),
                    "data": "0x" + keccak256(b"getUint(bytes32)")[:4].hex() + record["key"][2:]}
        if not isinstance(params, list) or len(params) != 2 or \
                not isinstance(params[0], dict) or \
                {**params[0], "to": params[0].get("to", "").lower()} != expected or \
                params[1] != block_tag:
            raise ValueError("pinned funding selector calldata mismatch")
        raw = transcript[2]["response"]["result"]
        if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x") or \
                record.get("funding_increase_factor_per_second") != int(raw, 16):
            raise ValueError("pinned funding selector decoded value mismatch")
    except (KeyError, TypeError, OverflowError) as error:
        raise ValueError("pinned funding selector malformed transcript") from error
    return record


def _market_increase_price_upper_bound(sidecar: dict[str, Any], oracle: dict[str, Any],
                                       balance: dict[str, Any],
                                       risk: dict[str, Any] | None) -> int | None:
    """Bound a same-side improving long's price without estimating impact magnitude."""
    order = sidecar["pinned_order"]
    market = sidecar["fixed_values"]["market"]
    if order["orderType"] != 2 or order["isLong"] is not True or \
            market["INDEX_TOKEN"].lower() != market["LONG_TOKEN"].lower() or \
            market["LONG_TOKEN"].lower() == market["SHORT_TOKEN"].lower() or \
            order["sizeDeltaUsd"] <= 0:
        return None
    prices = {token.lower(): pair for token, pair in zip(oracle["tokens"], oracle["prices"])}
    index = prices.get(market["INDEX_TOKEN"].lower())
    if not isinstance(index, list) or len(index) != 2 or \
            any(type(price) is not int or price <= 0 for price in index) or index[0] > index[1]:
        return None
    base_tokens = order["sizeDeltaUsd"] // index[1]
    if base_tokens == 0:
        return None
    if balance["values"]["use_oi_tokens_for_balance"]:
        token_oi = sidecar["fixed_values"]["open_interest"]
        long_oi = (token_oi["long:" + market["LONG_TOKEN"].lower()] +
                   token_oi["long:" + market["SHORT_TOKEN"].lower()])
        short_oi = (token_oi["short:" + market["LONG_TOKEN"].lower()] +
                    token_oi["short:" + market["SHORT_TOKEN"].lower()])
        delta = base_tokens
    else:
        if risk is None:
            return None
        long_oi = (risk["values"]["long_oi_usd_weth_collateral"] +
                   risk["values"]["long_oi_usd_usdc_collateral"])
        short_oi = (balance["values"]["short_oi_usd_weth_collateral"] +
                    balance["values"]["short_oi_usd_usdc_collateral"])
        delta = order["sizeDeltaUsd"]
    # On this side of the balance, increasing longs narrows the difference.
    # GMX's same-side pricing returns a nonnegative impact, so the long receives
    # at least the base token amount and executionPrice cannot exceed this bound.
    if not long_oi < short_oi or long_oi + delta > short_oi:
        return None
    return order["sizeDeltaUsd"] // base_tokens


def _zero_long_borrowing_fee(sidecar: dict[str, Any],
                             balance: dict[str, Any] | None,
                             risk: dict[str, Any] | None,
                             skip: dict[str, Any] | None,
                             clocks: dict[str, Any] | None) -> bool:
    if not balance or not skip or not clocks or \
            skip["skip_borrowing_fee_for_smaller_side"] is not True:
        return False
    market = sidecar["fixed_values"]["market"]
    if balance["values"]["use_oi_tokens_for_balance"]:
        oi = sidecar["fixed_values"]["open_interest"]
        long_oi = oi["long:" + market["LONG_TOKEN"].lower()] + oi[
            "long:" + market["SHORT_TOKEN"].lower()]
        short_oi = oi["short:" + market["LONG_TOKEN"].lower()] + oi[
            "short:" + market["SHORT_TOKEN"].lower()]
    elif risk:
        long_oi = (risk["values"]["long_oi_usd_weth_collateral"] +
                   risk["values"]["long_oi_usd_usdc_collateral"])
        short_oi = (balance["values"]["short_oi_usd_weth_collateral"] +
                    balance["values"]["short_oi_usd_usdc_collateral"])
    else:
        return False
    position = sidecar["position"]["value"]
    cumulative = sidecar["fixed_values"]["accrual"]["borrowing:long"]
    return (long_oi < short_oi and position["isLong"] is True and
            position["borrowingFactor"] == cumulative)


def verify_pinned_increase_executor(proof_path: Path, source_path: Path) -> dict[str, Any]:
    """Check exact-match executor source against the selected pinned runtime."""
    proof = json.loads(proof_path.read_text())
    source = json.loads(source_path.read_text())
    if proof.get("schema") != "GmxStep41PinnedExecutor" or \
            source.get("runtimeMatch") != "exact_match" or source.get("match") != "exact_match" or \
            source.get("address", "").lower() != proof.get("executor_address") or \
            str(source.get("chainId")) != str(proof.get("chain_id")):
        raise ValueError("pinned increase executor identity mismatch")
    transcript = proof.get("rpc_transcript")
    if not isinstance(transcript, list) or [x.get("method") for x in transcript] != [
            "eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_getCode",
            "eth_getBlockByNumber"] or \
            int(transcript[0]["response"]["result"], 16) != proof["chain_id"] or \
            any(int(transcript[i]["response"]["result"]["number"], 16) != proof["block_number"] or
                transcript[i]["response"]["result"]["hash"].lower() != proof["block_hash"]
                for i in (1, 4)):
        raise ValueError("pinned increase executor transcript mismatch")
    call = transcript[2]
    if call.get("params") != [{"to": proof["order_handler"], "data": "0x7f9011d7"},
                              hex(proof["block_number"])] or \
            int(call["response"]["result"], 16).to_bytes(32, "big")[-20:].hex() != proof["executor_address"][2:]:
        raise ValueError("pinned increase executor pointer mismatch")
    runtime = bytes.fromhex(source["runtimeBytecode"]["onchainBytecode"][2:])
    if transcript[3].get("params") != [proof["executor_address"], hex(proof["block_number"])] or \
            transcript[3]["response"]["result"].lower() != "0x" + runtime.hex() or \
            "0x" + keccak256(runtime).hex() != proof["runtime_code_keccak"] or \
            "0x" + hashlib.sha256(runtime).hexdigest() != proof["runtime_code_sha256"]:
        raise ValueError("pinned increase executor runtime mismatch")
    if source.get("compilation", {}).get("fullyQualifiedName") != \
            "contracts/order/IncreaseOrderExecutor.sol:IncreaseOrderExecutor":
        raise ValueError("unexpected increase executor source target")
    source_files = source.get("sources")
    metadata_files = source.get("metadata", {}).get("sources")
    if not isinstance(source_files, dict) or not isinstance(metadata_files, dict) or \
            set(source_files) != set(metadata_files) or \
            any("0x" + keccak256(item["content"].encode()).hex() !=
                metadata_files[path]["keccak256"] for path, item in source_files.items()) or \
            source.get("abi") != source.get("metadata", {}).get("output", {}).get("abi"):
        raise ValueError("increase executor source or ABI hash mismatch")
    required_fragments = {
        "contracts/swap/SwapUtils.sol": ("params.swapPathMarkets.length == 0",
                                          "params.amountIn < params.minOutputAmount"),
        "contracts/market/MarketUtils.sol": ("token == market.longToken || token == market.shortToken",),
        "contracts/order/IncreaseOrderUtils.sol": ("params.minOracleTimestamp < params.order.updatedAtTime()",
                                                    "Keys.REQUEST_EXPIRATION_TIME"),
        "contracts/position/IncreasePositionUtils.sol": (
            "cache.nextPositionSizeInUsd = params.position.sizeInUsd() + params.order.sizeDeltaUsd()",
            "params.position.setSizeInUsd(cache.nextPositionSizeInUsd)",
            "PositionUtils.validatePosition({",
            "params.order.sizeDeltaUsd().toInt256(),",
            "cache.baseSizeDeltaInTokens.toInt256()",
            "params.position.collateralToken(),\n            fees.feeAmountForPool.toInt256()"),
        "contracts/position/PositionUtils.sol": (
            "if (position.sizeInUsd() == 0 || position.sizeInTokens() == 0)",
            "revert Errors.InvalidPositionSizeValues"),
        "contracts/pricing/PositionPricingUtils.sol": (
            "fees.positionFeeFactor = dataStore.getUint(Keys.positionFeeFactorKey(market, balanceWasImproved))",
            "fees.positionFeeAmount = Precision.applyFactor(sizeDeltaUsd, fees.positionFeeFactor) / collateralTokenPrice.min",
            "uiFees.uiFeeReceiverFactor = uiFeeFactor == type(uint256).max",
            "uiFees.uiFeeAmount = Precision.applyFactor(sizeDeltaUsd, uiFees.uiFeeReceiverFactor) / collateralTokenPrice.min"),
    }
    if any(path not in source_files or any(fragment not in source_files[path]["content"]
                                          for fragment in fragments)
           for path, fragments in required_fragments.items()):
        raise ValueError("historical increase branch source unproved")
    return {"proof_sha256": _digest(proof_path), "source_sha256": _digest(source_path),
            "address": proof["executor_address"], "runtime_code_keccak": proof["runtime_code_keccak"]}


def verify_pinned_swap_handler(proof_path: Path, source_path: Path) -> dict[str, Any]:
    proof = json.loads(proof_path.read_text())
    source = json.loads(source_path.read_text())
    transcript = proof.get("rpc_transcript")
    if proof.get("schema") != "GmxStep41PinnedHandler" or \
            source.get("runtimeMatch") != "exact_match" or source.get("match") != "exact_match" or \
            source.get("address", "").lower() != proof.get("handler_address") or \
            str(source.get("chainId")) != str(proof.get("chain_id")) or \
            source.get("compilation", {}).get("fullyQualifiedName") != \
            "contracts/swap/SwapHandler.sol:SwapHandler" or \
            not isinstance(transcript, list) or [x.get("method") for x in transcript] != [
                "eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_getCode",
                "eth_getBlockByNumber"]:
        raise ValueError("pinned swap handler identity mismatch")
    if int(transcript[0]["response"]["result"], 16) != proof["chain_id"] or \
            any(int(transcript[i]["response"]["result"]["number"], 16) != proof["block_number"] or
                transcript[i]["response"]["result"]["hash"].lower() != proof["block_hash"]
                for i in (1, 4)) or \
            transcript[2].get("params") != [{"to": proof["order_handler"], "data": "0x8a53aaac"},
                                             hex(proof["block_number"])] or \
            int(transcript[2]["response"]["result"], 16).to_bytes(32, "big")[-20:].hex() != \
            proof["handler_address"][2:]:
        raise ValueError("pinned swap handler pointer mismatch")
    runtime = bytes.fromhex(source["runtimeBytecode"]["onchainBytecode"][2:])
    if transcript[3].get("params") != [proof["handler_address"], hex(proof["block_number"])] or \
            transcript[3]["response"]["result"].lower() != "0x" + runtime.hex() or \
            "0x" + keccak256(runtime).hex() != proof["runtime_code_keccak"] or \
            "0x" + hashlib.sha256(runtime).hexdigest() != proof["runtime_code_sha256"]:
        raise ValueError("pinned swap handler runtime mismatch")
    files, metadata_files = source.get("sources"), source.get("metadata", {}).get("sources")
    if not isinstance(files, dict) or not isinstance(metadata_files, dict) or \
            set(files) != set(metadata_files) or \
            any("0x" + keccak256(item["content"].encode()).hex() !=
                metadata_files[path]["keccak256"] for path, item in files.items()) or \
            source.get("abi") != source.get("metadata", {}).get("output", {}).get("abi") or \
            "params.swapPathMarkets.length == 0" not in files["contracts/swap/SwapUtils.sol"]["content"]:
        raise ValueError("pinned swap handler source mismatch")
    return {"proof_sha256": _digest(proof_path), "source_sha256": _digest(source_path),
            "address": proof["handler_address"], "runtime_code_keccak": proof["runtime_code_keccak"]}


class SelectedIncreaseDecisionAdapter:
    """Bind accepted artifacts and identify the first unproved execution gate."""

    def __init__(self, sidecar_path: Path, source_proof_path: Path,
                 source_manifest_path: Path, oracle_evidence_path: Path,
                 executor_proof_path: Path | None = None, executor_source_path: Path | None = None,
                 swap_proof_path: Path | None = None, swap_source_path: Path | None = None,
                 decision_config_path: Path | None = None,
                 risk_cells_path: Path | None = None,
                 balance_inputs_path: Path | None = None,
                 fee_clocks_path: Path | None = None,
                 borrowing_skip_path: Path | None = None,
                 funding_selector_path: Path | None = None):
        self.paths = (sidecar_path, source_proof_path, source_manifest_path,
                      oracle_evidence_path)
        self.sidecar, self.proof, self.manifest, self.oracle = (
            json.loads(path.read_text()) for path in self.paths)
        if self.sidecar.get("schema") != "GmxStep41ArchiveSidecar" or \
                self.proof.get("schema") != "GmxStep41SourcifySourceProof" or \
                self.manifest.get("schema") != "GmxStep41HistoricalSourceManifest" or \
                self.oracle.get("schema") != "GmxStep41RecordedOracleEvidence":
            raise ValueError("selected increase evidence schema mismatch")
        if self.proof.get("sidecar_sha256") != _digest(sidecar_path) or \
                self.proof.get("input_manifest_sha256") != _digest(source_manifest_path) or \
                self.manifest.get("sidecar_sha256") != _digest(sidecar_path) or \
                not self.proof.get("sourcify_exact_match_and_local_content_hashes_verified"):
            raise ValueError("selected increase source proof digest mismatch")
        if not self.oracle.get("source_and_scale_verified") or \
                not self.oracle.get("age_within_historical_max"):
            raise ValueError("selected increase oracle source or age unverified")
        self.executor_source = (verify_pinned_increase_executor(executor_proof_path,
                                                                 executor_source_path)
                                if executor_proof_path is not None and executor_source_path is not None
                                else None)
        self.swap_source = (verify_pinned_swap_handler(swap_proof_path, swap_source_path)
                            if swap_proof_path is not None and swap_source_path is not None else None)
        if decision_config_path is not None and self.executor_source is None:
            raise ValueError("pinned decision config requires verified increase executor source")
        self.decision_config = (verify_pinned_decision_config(decision_config_path,
                                                              executor_source_path, self.sidecar)
                                if decision_config_path is not None and self.executor_source is not None
                                and executor_source_path is not None else None)
        self.decision_config_sha256 = _digest(decision_config_path) if self.decision_config else None
        if risk_cells_path is not None and self.executor_source is None:
            raise ValueError("pinned risk cells require verified increase executor source")
        self.risk_cells = (verify_pinned_risk_cells(risk_cells_path, executor_source_path,
                                                    self.sidecar)
                           if risk_cells_path is not None else None)
        self.risk_cells_sha256 = _digest(risk_cells_path) if self.risk_cells else None
        if balance_inputs_path is not None and self.executor_source is None:
            raise ValueError("pinned balance inputs require verified increase executor source")
        self.balance_inputs = (verify_pinned_balance_inputs(balance_inputs_path,
                                                             executor_source_path, self.sidecar)
                               if balance_inputs_path is not None else None)
        self.balance_inputs_sha256 = _digest(balance_inputs_path) if self.balance_inputs else None
        if fee_clocks_path is not None and self.executor_source is None:
            raise ValueError("pinned fee clocks require verified increase executor source")
        self.fee_clocks = (verify_pinned_fee_clocks(fee_clocks_path, executor_source_path,
                                                    self.sidecar)
                           if fee_clocks_path is not None else None)
        self.fee_clocks_sha256 = _digest(fee_clocks_path) if self.fee_clocks else None
        if borrowing_skip_path is not None and self.executor_source is None:
            raise ValueError("pinned borrowing skip requires verified increase executor source")
        self.borrowing_skip = (verify_pinned_borrowing_skip(borrowing_skip_path,
                                                            executor_source_path, self.sidecar)
                               if borrowing_skip_path is not None else None)
        self.borrowing_skip_sha256 = _digest(borrowing_skip_path) if self.borrowing_skip else None
        if funding_selector_path is not None and self.executor_source is None:
            raise ValueError("pinned funding selector requires verified increase executor source")
        self.funding_selector = (verify_pinned_funding_selector(funding_selector_path,
                                                                 executor_source_path, self.sidecar)
                                 if funding_selector_path is not None else None)
        self.funding_selector_sha256 = _digest(funding_selector_path) if self.funding_selector else None
        for dependency_path in (executor_proof_path, swap_proof_path):
            if dependency_path is None:
                continue
            dependency = json.loads(dependency_path.read_text())
            pin = self.sidecar["pin"]
            deployment = self.sidecar["deployment"]
            if dependency.get("chain_id") != deployment["chain_id"] or \
                    dependency.get("block_number") != pin["number"] or \
                    dependency.get("block_hash", "").lower() != pin["recorded_hash"].lower() or \
                    dependency.get("order_handler", "").lower() != deployment["order_handler"].lower():
                raise ValueError("delegated contract proof differs from selected sidecar pin")

    def evaluate(self, preflight: dict[str, Any]) -> dict[str, Any]:
        only = lambda reason: {"status": "gmx_preflight_only", "reason": reason,
                               "rule_inventory": [name for name, _ in MARKET_INCREASE_RULES],
                               "decision_config_sha256": self.decision_config_sha256,
                               "risk_cells_sha256": self.risk_cells_sha256,
                               "balance_inputs_sha256": self.balance_inputs_sha256,
                               "fee_clocks_sha256": self.fee_clocks_sha256,
                               "borrowing_skip_sha256": self.borrowing_skip_sha256,
                               "funding_selector_sha256": self.funding_selector_sha256}
        if preflight.get("outcome") not in {"passed_preflight", "validation_error"}:
            return only("no_decoded_gmx_decision")
        pin = self.sidecar["pin"]
        if preflight.get("order_key", "").lower() != self.oracle.get("order_key") or \
                preflight.get("pin_block") != pin["number"] or \
                preflight.get("pin_block_hash", "").lower() != pin["recorded_hash"] or \
                preflight.get("pin_block_hash_after", "").lower() != pin["recorded_hash"]:
            return only("selected_order_or_pin_mismatch")
        if self.proof.get("pin_block") != pin["number"] or \
                self.proof.get("pin_hash") != pin["recorded_hash"]:
            return only("historical_source_pin_mismatch")
        if self.sidecar["router_gate"] != {
                "derived_latest_key": self.oracle["order_key"], "latest": True,
                "nonce": self.sidecar["router_gate"]["nonce"], "pending": True}:
            return only("pending_latest_proof_missing")
        request = preflight.get("on_chain_request")
        if not isinstance(request, dict) or request != self.sidecar["pinned_order"]:
            return only("pinned_request_not_equal")
        if self.sidecar.get("feature_flag", {}).get("disabled") is not False:
            return only("execute_order_feature_unproved")
        if self.executor_source is None:
            return only("historical_increase_executor_source_unproved")
        router_oracle = preflight.get("oracle") or {}
        if list(router_oracle.get("tokens", ())) != self.oracle["tokens"] or \
                [list(x) for x in router_oracle.get("prices", ())] != self.oracle["prices"] or \
                router_oracle.get("min_timestamp") != self.oracle["min_timestamp"] or \
                router_oracle.get("max_timestamp") != self.oracle["max_timestamp"]:
            return only("router_oracle_not_equal_to_evidence")
        order = self.sidecar["pinned_order"]
        market = self.sidecar["fixed_values"]["market"]
        proved = ["pending_latest_request", "order_field_identity",
                  "execute_order_feature_enabled", "oracle_price_source_and_age"]
        if order.get("orderType") != 2 or order.get("isLong") is not True:
            return only("unsupported_order_type_or_side")
        if order.get("swapPath") != [] or order.get("minOutputAmount") is None or \
                order.get("initialCollateralDeltaAmount") is None:
            return only("swap_branch_unproved")
        if order["initialCollateralDeltaAmount"] < order["minOutputAmount"]:
            return {**only("independent_swap_min_output_failed"),
                    "model_rule": "swap_path_and_min_output", "model_result": "reject"}
        proved.append("zero_swap_path_input_conditions")
        if self.swap_source is not None:
            proved.append("swap_path_and_min_output")
        if order["initialCollateralToken"].lower() not in {
                market["LONG_TOKEN"].lower(), market["SHORT_TOKEN"].lower()} or \
                market["INDEX_TOKEN"].lower() == "0x" + "0" * 40 or \
                market["MARKET_TOKEN"].lower() != order["market"].lower():
            return {**only("independent_market_or_collateral_failed"),
                    "model_rule": "market_and_collateral_token_valid", "model_result": "reject"}
        proved.append("collateral_token_membership")
        if self.decision_config is not None:
            if self.decision_config["values"]["is_market_disabled"]:
                return {**only("independent_market_disabled"),
                        "model_rule": "market_and_collateral_token_valid", "model_result": "reject"}
            proved.append("market_enabled")
            proved.append("market_and_collateral_token_valid")
        if self.oracle["min_timestamp"] < order["updatedAtTime"]:
            return {**only("independent_order_timestamp_failed"),
                    "model_rule": "order_valid_from_and_expiration", "model_result": "reject"}
        if self.decision_config is not None:
            # GMX's validFromTime condition excludes MarketIncrease via isMarketOrder.
            proved.append("market_order_valid_from_exemption")
            if self.oracle["max_timestamp"] > (order["updatedAtTime"] +
                                                 self.decision_config["values"]["request_expiration_time"]):
                return {**only("independent_request_expired"),
                        "model_rule": "order_valid_from_and_expiration", "model_result": "reject"}
            proved.append("order_valid_from_and_expiration")
        next_size_usd = self.sidecar["position"]["value"]["sizeInUsd"] + order["sizeDeltaUsd"]
        if next_size_usd == 0 or next_size_usd >= 2**256:
            return {**only("independent_invalid_next_position_size_usd"),
                    "model_rule": "post_update_position_validity", "model_result": "reject"}
        proved.append("nonzero_next_position_size_usd")
        if self.risk_cells is not None:
            risk = self.risk_cells["values"]
            if next_size_usd < risk["min_position_size_usd"]:
                return {**only("independent_min_position_size_failed"),
                        "model_rule": "minimum_position_and_collateral", "model_result": "reject"}
            proved.append("minimum_position_size_usd")
            next_oi_usd = (risk["long_oi_usd_weth_collateral"] +
                           risk["long_oi_usd_usdc_collateral"] + order["sizeDeltaUsd"])
            if next_oi_usd > risk["max_open_interest_long"]:
                return {**only("independent_max_open_interest_failed"),
                        "model_rule": "max_open_interest", "model_result": "reject"}
            proved.append("max_open_interest")
            # This selected two-token long uses USDC collateral. Fees change the
            # USDC pool; pending position impact does not change the WETH pool.
            if (market["INDEX_TOKEN"].lower() == market["LONG_TOKEN"].lower() and
                    market["LONG_TOKEN"].lower() != market["SHORT_TOKEN"].lower() and
                    order["initialCollateralToken"].lower() == market["SHORT_TOKEN"].lower()):
                index_price = {token.lower(): price for token, price in
                               zip(self.oracle["tokens"], self.oracle["prices"])}.get(
                                   market["INDEX_TOKEN"].lower())
                if not isinstance(index_price, list) or len(index_price) != 2 or \
                        any(not isinstance(price, int) or price <= 0 for price in index_price):
                    return only("reserve_oracle_price_unavailable")
                base_tokens = order["sizeDeltaUsd"] // index_price[1]
                oi_tokens = (self.sidecar["fixed_values"]["open_interest"][
                    "long:" + market["LONG_TOKEN"].lower()] +
                    self.sidecar["fixed_values"]["open_interest"][
                    "long:" + market["SHORT_TOKEN"].lower()] + base_tokens)
                pool_tokens = self.sidecar["fixed_values"]["liquidity"][
                    "pool:" + market["LONG_TOKEN"].lower()]
                reserved_usd = oi_tokens * index_price[1]
                pool_usd = pool_tokens * index_price[0]
                for factor_name, reason in (
                        ("reserve_factor_long", "independent_reserve_failed"),
                        ("open_interest_reserve_factor_long", "independent_oi_reserve_failed")):
                    max_reserved_usd = pool_usd * risk[factor_name] // 10**30
                    if reserved_usd > max_reserved_usd:
                        return {**only(reason), "model_rule": "reserve_and_open_interest_reserve",
                                "model_result": "reject"}
                proved.append("reserve_and_open_interest_reserve")
        price_upper_bound = (_market_increase_price_upper_bound(
            self.sidecar, self.oracle, self.balance_inputs, self.risk_cells)
            if self.balance_inputs is not None else None)
        if price_upper_bound is not None and price_upper_bound <= order["acceptablePrice"]:
            proved.append("acceptable_price_upper_bound")
        fee_diagnostics = {}
        if self.fee_clocks is not None:
            timestamp = self.fee_clocks["block_timestamp"]
            fee_diagnostics["funding_elapsed_seconds"] = (
                timestamp - self.fee_clocks["values"]["funding_updated_at"])
            fee_diagnostics["borrowing_elapsed_seconds_long"] = (
                timestamp - self.fee_clocks["values"]["borrowing_updated_at_long"])
        if self.funding_selector is not None:
            fee_diagnostics["funding_mode"] = (
                "adaptive_unreconstructed" if self.funding_selector[
                    "funding_increase_factor_per_second"] > 0 else "static_unreconstructed")
        if _zero_long_borrowing_fee(self.sidecar, self.balance_inputs,
                                    self.risk_cells, self.borrowing_skip, self.fee_clocks):
            fee_diagnostics["borrowing_fee_usd"] = 0
            fee_diagnostics["borrowing_fee_amount"] = 0
            proved.append("zero_long_borrowing_fee")
        if order.get("uiFeeReceiver", "").lower() == "0x" + "0" * 40 or \
                order.get("uiFeeFactor") == 0:
            fee_diagnostics["ui_fee_amount"] = 0
            proved.append("zero_ui_fee_amount")
        if "acceptable_price_upper_bound" in proved:
            collateral_price = {token.lower(): price for token, price in
                                zip(self.oracle["tokens"], self.oracle["prices"])}.get(
                                    order["initialCollateralToken"].lower())
            factor = self.sidecar["fixed_values"]["configuration"].get("position_fee:improved")
            if isinstance(collateral_price, list) and len(collateral_price) == 2 and \
                    type(collateral_price[0]) is int and collateral_price[0] > 0 and \
                    type(factor) is int and factor >= 0:
                fee_diagnostics["gross_position_fee_amount_before_discounts"] = (
                    order["sizeDeltaUsd"] * factor // 10**30 // collateral_price[0])
                proved.append("gross_position_fee_before_discounts")
        if self.sidecar.get("referral", {}).get("code") == "0x" + "0" * 64 and \
                self.sidecar.get("referral", {}).get("pro_trader_tier") == 0:
            proved.append("zero_referral_branch")
        if self.oracle["relationship"] == "observed_execution_transaction_prices_preceding_order_executed":
            return {**only("execution_oracle_and_creation_pin_not_equivalent"),
                    "proved_branches": proved}
        return {**only("independent_market_increase_rule_coverage_incomplete"),
                "proved_branches": proved,
                "execution_price_upper_bound": (
                    price_upper_bound if "acceptable_price_upper_bound" in proved else None),
                "fee_diagnostics": fee_diagnostics,
                "missing_rule_evidence": [name for name, _ in MARKET_INCREASE_RULES[4:]
                                          if name not in proved],
                "comparison_scope": "counterfactual_creation_block_prices"}
