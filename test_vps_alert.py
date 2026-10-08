from ha_notifier import send_trade_notification
send_trade_notification(
    question="London VPS Alert System Connected!",
    trade_size=10.0,
    expected_profit=0.50,
    edge_pct=5.0,
    execution_style="maker_taker",
    wallet_balance=28.39,
    market_id="0xTEST123",
    sync=True
)
print("NOTIFICATION SENT SUCCESSFULLY FROM LONDON VPS!")
