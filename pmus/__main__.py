import argparse
import os
import sys
from decimal import Decimal

from pmus.clob_venue import ClobVenueClient
from pmus.config import Settings
from pmus.engine import LiveEngine, ShadowEngine
from pmus.ledger import Ledger
from pmus.logging_setup import setup_logging
from pmus.report import generate_report
from pmus.risk import RiskManager
from pmus.venue import AuthenticatedClient, PublicGatewayClient

def probe_command(settings: Settings, venue: str = "global") -> None:
    if venue == "global":
        client = ClobVenueClient(settings)
        print("Fetching Polymarket Global reward markets...", flush=True)
        pairs = client.fetch_reward_markets(limit=20)
        print(f"Sampled global reward markets count: {len(pairs)}")
        for m, w in pairs[:3]:
            q_str = f" | Question: {m.question}" if getattr(m, "question", "") else ""
            print(f"  Market: {m.slug} | Event: {m.event_id}{q_str} | Tick: {m.tick} | MinQty: {m.min_qty}")

        print("\nFetching active incentive programs...", flush=True)
        print(f"Sampled incentive windows count: {len(pairs)}")
        for _, w in pairs[:3]:
            print(f"  Window: {w.slug} | Pool: ${w.pool_usd} | Discount: {w.discount} | Target: {w.target_size} | MaxSpread: {w.max_spread}")

        target_slug = pairs[0][0].slug if pairs else None
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
    else:
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
    settings = Settings.from_env()

    parser = argparse.ArgumentParser(prog="pmus", description="Polymarket US & Global Trading CLI")
    parser.add_argument("--venue", choices=["global", "us"], default=None, help="Trading venue (global or us)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe_parser = subparsers.add_parser("probe", help="Probe live public gateway data")
    probe_parser.add_argument("--venue", choices=["global", "us"], default=None, help="Trading venue (global or us)")

    run_parser = subparsers.add_parser("run", help="Run bot engine")
    run_parser.add_argument("--venue", choices=["global", "us"], default=None, help="Trading venue (global or us)")
    run_parser.add_argument("--mode", choices=["shadow", "live"], default=None, help="Run mode")
    run_parser.add_argument("--confirm-live", action="store_true", help="Explicit confirmation required to run live")
    run_parser.add_argument("--minutes", type=int, default=None, help="Run duration in minutes (omit for indefinite)")
    run_parser.add_argument("--seconds", type=int, default=None, help="Run duration in seconds (omit for indefinite)")

    report_parser = subparsers.add_parser("report", help="Generate performance report")
    report_parser.add_argument("--venue", choices=["global", "us"], default=None, help="Trading venue (global or us)")
    report_parser.add_argument("--since", default="1h", help="Time window (e.g. 1h, 24h)")

    flatten_parser = subparsers.add_parser("flatten", help="Cancel open orders and close positions")
    flatten_parser.add_argument("--venue", choices=["global", "us"], default=None, help="Trading venue (global or us)")
    flatten_parser.add_argument("--slug", help="Optional specific market slug to flatten")

    dashboard_parser = subparsers.add_parser("dashboard", help="Launch Streamlit Web HUD")
    dashboard_parser.add_argument("--port", type=int, default=None, help="Optional port for Streamlit")


    args = parser.parse_args()
    venue = args.venue or settings.venue
    mode = getattr(args, "mode", None) or settings.mode
    confirm_live = getattr(args, "confirm_live", False) or (os.getenv("PMUS_CONFIRM_LIVE", "").strip().lower() in ("1", "true", "yes"))
    if venue != settings.venue or mode != settings.mode:
        import dataclasses
        settings = dataclasses.replace(settings, venue=venue, mode=mode)

    if args.command == "probe":
        probe_command(settings, venue=venue)
    elif args.command == "run":
        logger = setup_logging(logs_dir=settings.logs_dir)
        ledger = Ledger(settings.data_dir / "ledger.db")
        duration = args.seconds if args.seconds is not None else ((args.minutes * 60) if args.minutes is not None else None)

        if venue == "global":
            clob_client = ClobVenueClient(settings)
            if mode == "live":
                if not confirm_live:
                    sys.exit("Error: Live mode requires explicit confirmation via --confirm-live flag or PMUS_CONFIRM_LIVE=1.")
                if not clob_client.is_available:
                    sys.exit("Error: POLYGON_PRIVATE_KEY is required for live mode on Polymarket Global.")

                risk_manager = RiskManager(settings)
                engine = LiveEngine(
                    settings=settings,
                    ledger=ledger,
                    public_client=clob_client,
                    auth_client=clob_client,
                    risk_manager=risk_manager,
                    logger=logger,
                )
                engine.run(duration_seconds=duration)
            else:
                engine = ShadowEngine(settings=settings, ledger=ledger, client=clob_client, logger=logger)
                engine.run(duration_seconds=duration)
        else:
            public_client = PublicGatewayClient(base_url=settings.gateway_url)
            if mode == "live":
                if not confirm_live:
                    sys.exit("Error: Live mode requires explicit confirmation via --confirm-live flag or PMUS_CONFIRM_LIVE=1.")
                if not settings.key_id or not settings.secret_key:
                    sys.exit("Error: PMUS_KEY_ID and PMUS_SECRET_KEY are required for live mode.")

                auth_client = AuthenticatedClient(key_id=settings.key_id, secret_key=settings.secret_key)
                risk_manager = RiskManager(settings)
                engine = LiveEngine(
                    settings=settings,
                    ledger=ledger,
                    public_client=public_client,
                    auth_client=auth_client,
                    risk_manager=risk_manager,
                    logger=logger,
                )
                engine.run(duration_seconds=duration)
            else:
                engine = ShadowEngine(settings=settings, ledger=ledger, client=public_client, logger=logger)
                engine.run(duration_seconds=duration)

    elif args.command == "report":
        ledger = Ledger(settings.data_dir / "ledger.db")
        if venue == "global":
            client = ClobVenueClient(settings)
        else:
            client = PublicGatewayClient(base_url=settings.gateway_url)
        print(generate_report(ledger, since_str=args.since, client=client))

    elif args.command == "flatten":
        if venue == "global":
            clob_client = ClobVenueClient(settings)
            if not clob_client.is_available:
                sys.exit("Error: POLYGON_PRIVATE_KEY is required to flatten positions on global venue.")
            open_orders = clob_client.get_open_orders()
            pos_by_slug = {p.slug: p for p in clob_client.get_positions()}
            if args.slug:
                clob_client.cancel_all_scoped([args.slug])
                pos = pos_by_slug.get(args.slug)
                if pos and pos.net_qty != Decimal("0"):
                    try:
                        bbo = clob_client.fetch_bbo(args.slug)
                    except Exception:
                        bbo = None
                    order = clob_client.close_position_safe(args.slug, pos, bbo=bbo)
                    if order:
                        print(f"Placed passive limit order {order.order_id} ({order.side} {order.qty_open} @ {order.price}) to safely flatten {args.slug}")
                    else:
                        print(f"Closed position for {args.slug}")
                else:
                    clob_client.close_position(args.slug)
                    print(f"Flattened market {args.slug}")
            else:
                slugs = list({o.slug for o in open_orders} | {p.slug for p in pos_by_slug.values() if p.net_qty != Decimal("0")})
                if slugs:
                    clob_client.cancel_all_scoped(slugs)
                    for pos in [pos_by_slug[s] for s in slugs if s in pos_by_slug]:
                        try:
                            bbo = clob_client.fetch_bbo(pos.slug)
                        except Exception:
                            bbo = None
                        order = clob_client.close_position_safe(pos.slug, pos, bbo=bbo)
                        if order:
                            print(f"Placed passive limit order {order.order_id} ({order.side} {order.qty_open} @ {order.price}) to safely flatten {pos.slug}")
                        else:
                            print(f"Closed position for {pos.slug}")
                print(f"Flattened all {len(slugs)} active markets")
        else:
            if not settings.key_id or not settings.secret_key:
                sys.exit("Error: PMUS_KEY_ID and PMUS_SECRET_KEY are required to flatten live positions.")
            auth_client = AuthenticatedClient(key_id=settings.key_id, secret_key=settings.secret_key)
            public_client = PublicGatewayClient(base_url=settings.gateway_url)

            positions = auth_client.get_positions()
            pos_by_slug = {p.slug: p for p in positions}

            if args.slug:
                auth_client.cancel_all_scoped([args.slug])
                pos = pos_by_slug.get(args.slug)
                if pos and pos.net_qty != Decimal("0"):
                    try:
                        bbo = public_client.fetch_bbo(args.slug)
                    except Exception:
                        bbo = None
                    order = auth_client.close_position_safe(args.slug, pos, bbo=bbo)
                    if order:
                        print(f"Placed passive limit order {order.order_id} ({order.side} {order.qty_open} @ {order.price}) to safely flatten {args.slug}")
                    else:
                        print(f"Closed position for {args.slug}")
                else:
                    auth_client.close_position(args.slug)
                    print(f"Flattened market {args.slug}")
            else:
                slugs = [p.slug for p in positions if p.net_qty != Decimal("0")]
                if slugs:
                    auth_client.cancel_all_scoped(slugs)
                    for pos in [pos_by_slug[s] for s in slugs]:
                        try:
                            bbo = public_client.fetch_bbo(pos.slug)
                        except Exception:
                            bbo = None
                        order = auth_client.close_position_safe(pos.slug, pos, bbo=bbo)
                        if order:
                            print(f"Placed passive limit order {order.order_id} ({order.side} {order.qty_open} @ {order.price}) to safely flatten {pos.slug}")
                        else:
                            print(f"Closed position for {pos.slug}")
                print(f"Flattened all {len(slugs)} active markets")
    elif args.command == "dashboard":
        import subprocess
        from pathlib import Path

        dashboard_path = Path(__file__).resolve().parent / "dashboard.py"
        cmd = [sys.executable, "-m", "streamlit", "run", str(dashboard_path)]
        if getattr(args, "port", None):
            cmd.extend(["--server.port", str(args.port)])
        subprocess.run(cmd)

if __name__ == "__main__":
    main()

