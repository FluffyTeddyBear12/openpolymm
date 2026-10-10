import argparse
import sys
from decimal import Decimal

from pmus.config import Settings
from pmus.venue import PublicGatewayClient

def probe_command(settings: Settings) -> None:
    client = PublicGatewayClient(base_url=settings.gateway_url)

    print("Fetching active markets...", flush=True)
    markets = client.fetch_markets(active=True, closed=False, limit=20)
    print(f"Sampled active markets count: {len(markets)}")
    for m in markets[:3]:
        print(f"  Market: {m.slug} | Cat: {m.category} | Tick: {m.tick} | MinQty: {m.min_qty}")

    print("\nFetching active incentive programs...", flush=True)
    windows = client.fetch_incentives(status="active", page_size=20)
    print(f"Sampled incentive windows count: {len(windows)}")
    for w in windows[:3]:
        print(f"  Window: {w.slug} | Pool: ${w.pool_usd} | Discount: {w.discount} | Target: {w.target_size} | MaxSpread: {w.max_spread}")

    target_slug = windows[0].slug if windows else (markets[0].slug if markets else None)
    if target_slug:
        print(f"\nFetching order book for {target_slug}...", flush=True)
        book = client.fetch_book(target_slug)
        best_bid = book.bids[0] if book.bids else None
        best_ask = book.asks[0] if book.asks else None
        spread = (best_ask.price - best_bid.price) if (best_bid and best_ask) else Decimal("0")
        print(f"  Book for {book.slug}: bids={len(book.bids)}, asks={len(book.asks)}, spread={spread}")
        if best_bid:
            print(f"  Best Bid: px={best_bid.price}, qty={best_bid.qty}")
        if best_ask:
            print(f"  Best Ask: px={best_ask.price}, qty={best_ask.qty}")

def main() -> None:
    parser = argparse.ArgumentParser(prog="pmus", description="Polymarket US Trading CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("probe", help="Probe live public gateway data")

    run_parser = subparsers.add_parser("run", help="Run bot engine")
    run_parser.add_argument("--mode", choices=["shadow", "live"], default="shadow", help="Run mode")
    run_parser.add_argument("--minutes", type=int, default=10, help="Run duration in minutes")

    report_parser = subparsers.add_parser("report", help="Generate performance report")
    report_parser.add_argument("--since", default="1h", help="Time window (e.g. 1h, 24h)")

    args = parser.parse_args()
    settings = Settings.from_env()

    if args.command == "probe":
        probe_command(settings)
    elif args.command == "run":
        if args.mode == "live":
            sys.exit("Error: Live trading is not implemented in this phase.")
        sys.exit("Error: Engine run will be wired in Phase 3.")
    elif args.command == "report":
        sys.exit("Error: Reporting will be wired in Phase 3.")

if __name__ == "__main__":
    main()
