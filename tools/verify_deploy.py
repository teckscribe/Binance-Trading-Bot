"""Pre-deploy consistency check: will the bot actually start?"""
import os, sys, ast, glob, io, importlib, warnings, logging
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # relocation-proof; was a hardcoded absolute path
sys.path.insert(0, PROJECT); os.chdir(PROJECT)
warnings.filterwarnings("ignore"); logging.disable(logging.WARNING)

FAIL = []
def ok(msg):   print("  [OK]   %s" % msg)
def bad(msg):  print("  [FAIL] %s" % msg); FAIL.append(msg)

print("=" * 70)
print("1. DELETED MODULES — nothing may still import them")
print("=" * 70)
gone = ["llm_regime", "llm_advisor", "oi_breakouts", "weekend_anomaly"]
for f in ["modules/llm_regime.py","modules/llm_advisor.py",
          "modules/strategies/oi_breakouts.py","modules/strategies/weekend_anomaly.py"]:
    (bad if os.path.exists(f) else ok)("%s %s" % (f, "STILL PRESENT" if os.path.exists(f) else "deleted"))

hits = []
for p in glob.glob("**/*.py", recursive=True):
    if "venv" in p or "__pycache__" in p or ".git" in p: continue
    src = io.open(p, encoding="utf-8", errors="replace").read()
    try: tree = ast.parse(src)
    except SyntaxError as e:
        bad("%s does not parse: %s" % (p, e)); continue
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):     mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module: mods = [node.module]
        for m in mods:
            for g in gone:
                if g in m: hits.append("%s:%d imports %s" % (p, node.lineno, m))
if hits:
    for h in hits: bad(h)
else:
    ok("no module imports any deleted module")

print()
print("=" * 70)
print("2. EVERY PROJECT FILE COMPILES")
print("=" * 70)
bad_c = []
for p in glob.glob("**/*.py", recursive=True):
    if "venv" in p or "__pycache__" in p or ".git" in p: continue
    try: compile(io.open(p, encoding="utf-8", errors="replace").read(), p, "exec")
    except SyntaxError as e: bad_c.append("%s: %s" % (p, e))
if bad_c:
    for b in bad_c: bad(b)
else:
    ok("all project .py files compile")

print()
print("=" * 70)
print("3. IMPORT CHAIN (what the bot does at startup)")
print("=" * 70)
for m in ["modules.regime_engine", "modules.risk_engine", "modules.order_engine",
          "modules.strategies.strategy_factory", "modules.strategy_overrides",
          "modules.ml_engine", "live_logger", "web_server"]:
    try:
        importlib.import_module(m); ok("import %s" % m)
    except Exception as e:
        bad("import %s -> %s: %s" % (m, type(e).__name__, e))

print()
print("=" * 70)
print("4. ENGINE STATE")
print("=" * 70)
try:
    from modules.strategies.strategy_factory import StrategyFactory
    from modules.regime_engine import REGIME_STRATEGY_PERMISSIONS as P
    ids = [s.STRATEGY_ID for s in StrategyFactory.get_all()]
    ok("factory loads: %s" % ids)
    for r in ["BULL_TREND","BEAR_TREND","RANGING","OVERHEATED","OVERSOLD"]:
        g = [s.STRATEGY_ID for s in StrategyFactory.get_permitted({"regime": r}, P)]
        print("         %-12s -> %s" % (r, g or "(none)"))
    from modules.order_engine import update_stop_order
    ok("order_engine.update_stop_order present (Fix B)")
    import live_scanner as ls
    ok("live_scanner imports; MANAGE_ON_BAR_CLOSE=%s" % ls.MANAGE_ON_BAR_CLOSE)
    if hasattr(ls, "_manage_on_bar_close"):
        src = io.open("live_scanner.py", encoding="utf-8").read()
        if "cur_sl" in src and 'pos.get("sl_price")' in src:
            ok("paper path tests the CURRENT stop (trailed), not entry stop")
        else:
            bad("paper path still tests initial_sl_price only")
except Exception as e:
    bad("engine state: %s: %s" % (type(e).__name__, e))

print()
print("=" * 70)
print("RESULT: %s" % ("READY TO DEPLOY" if not FAIL else "%d PROBLEM(S)" % len(FAIL)))
print("=" * 70)
for f in FAIL: print("  - %s" % f)
