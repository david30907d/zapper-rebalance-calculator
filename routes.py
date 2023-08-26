import copy
import os
import random
from collections import defaultdict

import requests
from tenacity import retry, stop_after_attempt, wait_fixed

from rebalance_server.apr_utils import convert_apy_to_apr
from rebalance_server.main import load_evm_raw_positions


def get_debank_data():
    evm_positions = load_evm_raw_positions(
        "debank",
        [
            "0xC6a58A8494E61fc4EF04F6075c4541C9664ADcC9",
            "0x271E3409093f7ECffFB2a1C82e5E87B2ecB3E310",
            "0x549caec2C863a04853Fb829aac4190E1B50df0Cc",
            "0xE66c4EA218Cdb8DCbCf3f605ed1aC29461CBa4b8",
        ],
        useCache=True if random.random() > 0.2 else False,
    )
    token_metadata_table: dict[str, dict] = {}
    for position in evm_positions["data"]["result"]["data"]:
        for portfolio_item in position["portfolio_item_list"]:
            for token in portfolio_item["detail"].get(
                "reward_token_list", []
            ) + portfolio_item["detail"].get("supply_token_list", []):
                payload = {
                    "img": token["logo_url"],
                    "price": token["price"],
                    "symbol": token["symbol"],
                    "chain": token["chain"],
                }
                token_metadata_table[f'{token["chain"]}:{token["id"]}'] = payload
                if token["symbol"] == "WETH" and token["chain"] != "bsc":
                    ethPayload = copy.copy(payload)
                    ethPayload["symbol"] = "ETH"
                    ethPayload[
                        "img"
                    ] = "https://static.debank.com/image/token/logo_url/eth/935ae4e4d1d12d59a99717a24f2540b5.png"
                    token_metadata_table[
                        f"{token['chain']}:0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                    ] = ethPayload
    return token_metadata_table


def get_APR_composition(portfolio_name: str):
    apr_composition = {}
    if portfolio_name == "permanent_portfolio":
        equilibria_market_addrs = {
            "Equilibria-GDAI": {
                "pool_addr": "0xa0192f6567f8f5DC38C53323235FD08b318D2dcA",
                "ratio": 0.25,
            },
            "Equilibria-GLP": {
                "pool_addr": "0x7D49E5Adc0EAAD9C027857767638613253eF125f",
                "ratio": 0.25,
            },
            "Equilibria-RETH": {
                "pool_addr": "0x14FbC760eFaF36781cB0eb3Cb255aD976117B9Bd",
                "ratio": 0.25,
            },
        }
        for key, pool_metadata in equilibria_market_addrs.items():
            apr_composition[key] = _fetch_equilibria_APR_composition(
                pool_metadata["pool_addr"], ratio=pool_metadata["ratio"]
            )
        apr_composition["SushSwap-DpxETH"] = _fetch_sushiswap_APR_composition(
            "0x0c1cf6883efa1b496b01f654e247b9b419873054", ratio=0.25
        )
        return apr_composition
    raise NotImplementedError(f"Portfolio {portfolio_name} is not implemented")


@retry(stop=stop_after_attempt(5), wait=wait_fixed(5))
def fetch_1inch_swap_data(
    chain_id: int,
    from_token_address: str,
    to_token_address: str,
    amount: str,
    from_address: str,
    slippage: int,
):
    url = f"https://api.1inch.dev/swap/v5.2/{chain_id}/swap?src={from_token_address}&dst={to_token_address}&amount={amount}&from={from_address}&slippage={slippage}&disableEstimate=true"
    headers = {
        "Authorization": f"Bearer {os.getenv('ONE_INCH_API_KEY')}",
        "Accept": "application/json",
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # Will trigger a retry if HTTP error
    return response.json()


def _fetch_equilibria_APR_composition(pool_addr: str, ratio: float) -> float:
    result = defaultdict(lambda: defaultdict(float))
    equilibria_chain_info_map = requests.get("https://equilibria.fi/api/chain-info-map")
    if equilibria_chain_info_map.status_code != 200:
        raise Exception("Failed to fetch equilibria chain info map")
    equilibria_chain_info_map = equilibria_chain_info_map.json()
    result["Underlying APY"]["token"] = []
    pool_info = [
        pl
        for pl in equilibria_chain_info_map["42161"]["poolInfos"]
        if pl["market"] == pool_addr and pl["shutdown"] is False
    ][0]
    for category, keys in {
        "Swap Fee": ["swapFeeApy"],
        "Underlying APY": ["underlyingApy", "lpRewardApy"],
        "PENDLE": ["pendleApy"],
    }.items():
        for key in keys:
            result[category]["APR"] += (
                convert_apy_to_apr(pool_info["marketInfo"][key]) * ratio
            )
        if category == "PENDLE":
            result["PENDLE"][
                "token"
            ] = "arb:0x0c880f6761F1af8d9Aa9C466984b80DAb9a8c9e8".lower()
            result["PENDLE"]["APR"] = (
                convert_apy_to_apr(
                    pool_info["pendleBoostedApy"]
                    - pool_info["pendleApy"]
                    + pool_info["pendleBaseBoostableApy"]
                )
                * ratio
            )
            result["EQB"][
                "token"
            ] = "arb:0xBfbCFe8873fE28Dfa25f1099282b088D52bbAD9C".lower()
            result["EQB"]["APR"] = convert_apy_to_apr(pool_info["eqbApy"]) * ratio
            # TODO(david): uncomment xEQB once we can redeem xEQB for user
            # result["xEQB"]["token"] = "0x96C4A48Abdf781e9c931cfA92EC0167Ba219ad8E".lower()
            # result["xEQB"]["APR"] = convert_apy_to_apr(pool_info["xEqbApy"])
        elif category == "Underlying APY":
            for reward in pool_info["marketInfo"]["underlyingRewardApyBreakdown"]:
                result[category]["token"].append(
                    f'arb:{reward["asset"]["address"].lower()}'
                )
            result[category]["token"].append(
                f'arb:{pool_info["marketInfo"]["basePricingAsset"]["address"]}'
            )
    return result


def _fetch_sushiswap_APR_composition(pool_addr: str, ratio: float) -> float:
    result = defaultdict(lambda: defaultdict(float))
    pool_res = requests.get(f"https://pools.sushi.com/api/v0/42161/{pool_addr}")
    if pool_res.status_code != 200:
        raise Exception("Failed to fetch kava sushiswap APR")
    pool_json = pool_res.json()
    for incentive in pool_json["incentives"]:
        if incentive["apr"] > 0:
            symbol = incentive["rewardToken"]["symbol"]
            result[symbol][
                "token"
            ] = f'arb:{incentive["rewardToken"]["address"].lower()}'
            result[symbol]["APR"] = incentive["apr"] * ratio

    result["Swap Fee"]["APR"] = pool_json["feeApr1d"] * ratio
    return result
