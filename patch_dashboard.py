import sys
import re

with open(r'd:\neststock\scripts\polymarket_bot\dashboard.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Patch format_risk_ipc_payload
old_def = '''def format_risk_ipc_payload(
    capital: Optional[float],
    exposure_pct: float,
    max_concurrent: int,
    min_edge_pct: float = 0.20,
    taker_fee_bps: int = 35
) -> dict:'''

new_def = '''def format_risk_ipc_payload(
    capital: Optional[float],
    exposure_pct: float,
    max_concurrent: int,
    min_edge_pct: float = 0.20,
    taker_fee_bps: int = 35,
    execution_mode: str = "Paper Trading",
    live_wager_cap: float = 1.0
) -> dict:'''

content = content.replace(old_def, new_def)

old_payload = '''    payload = {
        "max_exposure_pct": pct_fraction,
        "max_concurrent_positions": clamp_concurrent_markets(max_concurrent),
        "min_edge_pct": max(0.0005, min(0.05, edge_fraction)),
        "taker_fee_bps": fee_val,
        "timestamp": time.time()
    }'''

new_payload = '''    payload = {
        "max_exposure_pct": pct_fraction,
        "max_concurrent_positions": clamp_concurrent_markets(max_concurrent),
        "min_edge_pct": max(0.0005, min(0.05, edge_fraction)),
        "taker_fee_bps": fee_val,
        "execution_mode": execution_mode,
        "live_wager_cap": live_wager_cap,
        "timestamp": time.time()
    }'''

content = content.replace(old_payload, new_payload)

# Patch the UI fields
ui_old1 = '''    initial_taker_fee_bps = int(initial_state.get("taker_fee_bps", 35) if initial_state else 35)'''
ui_new1 = '''    initial_taker_fee_bps = int(initial_state.get("taker_fee_bps", 35) if initial_state else 35)
    initial_execution_mode = str(initial_state.get("execution_mode", "Paper Trading") if initial_state else "Paper Trading")
    initial_live_wager_cap = float(initial_state.get("live_wager_cap", 1.0) if initial_state else 1.0)
'''
content = content.replace(ui_old1, ui_new1)

ui_old2 = '''    if "taker_fee_bps_input" not in st.session_state:
        st.session_state["taker_fee_bps_input"] = int(initial_taker_fee_bps)'''
ui_new2 = '''    if "taker_fee_bps_input" not in st.session_state:
        st.session_state["taker_fee_bps_input"] = int(initial_taker_fee_bps)
    if "execution_mode_input" not in st.session_state:
        st.session_state["execution_mode_input"] = str(initial_execution_mode)
    if "live_wager_cap_input" not in st.session_state:
        st.session_state["live_wager_cap_input"] = float(initial_live_wager_cap)
'''
content = content.replace(ui_old2, ui_new2)


ui_old3 = '''            col_c4, col_c5, col_c6 = st.columns([1.5, 1.5, 1.2])'''

ui_new3 = '''            col_m1, col_m2 = st.columns(2)
            with col_m1:
                current_mode_val = st.session_state.get("execution_mode_input", initial_execution_mode)
                mode_index = 0 if current_mode_val == "Paper Trading" else 1
                new_execution_mode = st.radio("Execution Mode", ["Paper Trading", "Live Trading"], index=mode_index, horizontal=True)
            with col_m2:
                current_wager_val = float(st.session_state.get("live_wager_cap_input", initial_live_wager_cap))
                wager_opts = [1.0, 5.0, 10.0, 25.0]
                wager_index = wager_opts.index(current_wager_val) if current_wager_val in wager_opts else 0
                new_live_wager_cap = st.select_slider("Live Wager Cap ($)", options=wager_opts, value=wager_opts[wager_index])

            col_c4, col_c5, col_c6 = st.columns([1.5, 1.5, 1.2])'''

content = content.replace(ui_old3, ui_new3)

ui_old4 = '''                        payload = format_risk_ipc_payload(
                            applied_capital,
                            new_exposure_pct,
                            new_max_concurrent,
                            min_edge_pct=new_min_edge_pct,
                            taker_fee_bps=new_taker_fee_bps
                        )'''

ui_new4 = '''                        payload = format_risk_ipc_payload(
                            applied_capital,
                            new_exposure_pct,
                            new_max_concurrent,
                            min_edge_pct=new_min_edge_pct,
                            taker_fee_bps=new_taker_fee_bps,
                            execution_mode=new_execution_mode,
                            live_wager_cap=new_live_wager_cap
                        )'''

content = content.replace(ui_old4, ui_new4)

ui_old5 = '''                        payload = format_risk_ipc_payload(
                            None,
                            new_exposure_pct,
                            new_max_concurrent,
                            min_edge_pct=new_min_edge_pct,
                            taker_fee_bps=new_taker_fee_bps
                        )'''

ui_new5 = '''                        payload = format_risk_ipc_payload(
                            None,
                            new_exposure_pct,
                            new_max_concurrent,
                            min_edge_pct=new_min_edge_pct,
                            taker_fee_bps=new_taker_fee_bps,
                            execution_mode=new_execution_mode,
                            live_wager_cap=new_live_wager_cap
                        )'''
content = content.replace(ui_old5, ui_new5)

ui_old6 = '''                        fresh_state["taker_fee_bps"] = payload["taker_fee_bps"]'''
ui_new6 = '''                        fresh_state["taker_fee_bps"] = payload["taker_fee_bps"]
                        fresh_state["execution_mode"] = payload.get("execution_mode", "Paper Trading")
                        fresh_state["live_wager_cap"] = payload.get("live_wager_cap", 1.0)'''

content = content.replace(ui_old6, ui_new6)

ui_old7 = '''                    st.session_state["taker_fee_bps_input"] = int(new_taker_fee_bps)'''
ui_new7 = '''                    st.session_state["taker_fee_bps_input"] = int(new_taker_fee_bps)
                    st.session_state["execution_mode_input"] = str(new_execution_mode)
                    st.session_state["live_wager_cap_input"] = float(new_live_wager_cap)'''

content = content.replace(ui_old7, ui_new7)

with open(r'd:\neststock\scripts\polymarket_bot\dashboard.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Patched dashboard.py')
