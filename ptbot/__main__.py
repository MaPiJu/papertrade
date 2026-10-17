"""Command line: python -m ptbot <command>  (python -m ptbot -h for help)."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .constants import DOMAIN_CANCEL, DOMAIN_EXCHANGE, DOMAIN_STAKING, SCALE

log = logging.getLogger("ptbot")


def setup_logging(state_dir: Path, verbose: bool = False) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(state_dir / "bot.log")):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_live(settings):
    from .chain import HyperEvm, resolve_domains
    from .hyperliquid import HyperliquidInfo
    from .papertrade_api import PaperTradeApi
    from .venue import LiveVenue

    evm = HyperEvm(settings.rpc_url)
    try:
        domains = resolve_domains(evm, {"exchange": DOMAIN_EXCHANGE, "staking": DOMAIN_STAKING,
                                        "cancel": DOMAIN_CANCEL})
    except RuntimeError as e:   # never sign with a guessed domain because the RPC was down
        raise SystemExit(f"Domaines EIP-712 illisibles: {e}")
    return LiveVenue(settings.wallets, PaperTradeApi(settings.api_url), evm, HyperliquidInfo(settings.hl_url),
                     settings.hl_url, domains, settings.state_dir, settings.use_session_key)


def _try(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # probe keeps going and reports what failed
        return f"ERREUR: {e}"


def cmd_probe(settings, args) -> None:
    """Read-only: dumps every raw response so the API mapping can be checked."""
    from .chain import HyperEvm
    from .constants import EXCHANGE_CONTRACT, PAPER_STAKING
    from .hyperliquid import HyperliquidInfo
    from .papertrade_api import PaperTradeApi
    from .positions import live_positions

    api, evm, hl = PaperTradeApi(settings.api_url), HyperEvm(settings.rpc_url), HyperliquidInfo(settings.hl_url)
    out_dir = settings.state_dir / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "clock_offset_vs_papertrade_s": _try(lambda: round(time.time() - api.server_time(), 1)),
        "clock_offset_vs_hyperevm_s": _try(lambda: round(time.time() - evm.block_time(), 1)),
        "profit_queue_total_usd": _try(api.queue_total),
        "state_trading": _try(api.trading_state),
        "minimums_usd": _try(api.minimums),
        "eip712_exchange_onchain": _try(evm.eip712_domain, EXCHANGE_CONTRACT),
        "eip712_staking_onchain": _try(evm.eip712_domain, PAPER_STAKING),
        "eip712_defaults": {"exchange": DOMAIN_EXCHANGE, "staking": DOMAIN_STAKING},
        "mid": _try(hl.bbo_mid, settings.params.coin),
        "emission_paper_per_usd": _try(evm.emission_per_usd),
        "emission_site_formula": _try(api.emission),
        "wallets": {},
    }
    for label, w in settings.wallets.items():
        trades = _try(api.account_trades, w.address)
        live = _try(api.account_live, w.address)
        report["wallets"][label] = {
            "address": w.address,
            "balances_return_bytes": _try(evm.balances_return_size, w.address),
            "trades_order": api.trades_order(w.address),
            "trades_count": len(trades) if isinstance(trades, list) else trades,
            "usdc_papertrade": _try(evm.usdc_balance, w.address),
            "usdc_queued": _try(evm.queued_usdc, w.address),
            "paper_unstaked": _try(lambda a: evm.paper_balance_raw(a) / SCALE, w.address),
            "usdc_hypercore_spot": _try(hl.spot_usdc, w.address),
            "usdc_hypercore_perp": _try(hl.perp_withdrawable, w.address),
            "deposit_address_env": w.deposit_address,
            "deposit_address_api": _try(api.deposit_address, w.address),
            "open_positions_live": (_try(lambda x: [p.__dict__ for p in live_positions(x)], live)
                                    if isinstance(live, dict) else live),
            "closed_in_history": (_try(lambda t: len({r.get("positionId") for r in t if isinstance(r, dict)}), trades)
                                  if isinstance(trades, list) else trades),
            "account_live_raw": live,
            "trades_raw_first_page": trades[:40] if isinstance(trades, list) else None,
            "funding_raw": _try(api.get, f"/query/accounts/{w.address.lower()}/funding"),
        }
    path = out_dir / "probe.json"
    path.write_text(json.dumps(report, indent=1, default=str))
    summary = {k: v for k, v in report.items() if k not in ("state_trading", "wallets")}
    print(json.dumps(summary, indent=1, default=str))
    for label, wr in report["wallets"].items():
        print(f"\nWallet {label} {wr['address']}")
        for k in ("balances_return_bytes", "trades_order", "trades_count", "usdc_papertrade", "usdc_queued",
                  "paper_unstaked", "usdc_hypercore_spot", "usdc_hypercore_perp", "deposit_address_env",
                  "deposit_address_api", "closed_in_history", "open_positions_live"):
            print(f"  {k:24} {wr[k]}")
    print(f"\nRéponses brutes complètes: {path}")
    for label, wr in report["wallets"].items():
        if wr["deposit_address_env"] and isinstance(wr["deposit_address_api"], str) \
                and wr["deposit_address_api"].lower() != wr["deposit_address_env"].lower():
            print(f"ATTENTION wallet {label}: DEPOSIT_ADDRESS_{label} ne correspond pas à l'adresse donnée par l'API")


def cmd_status(settings, args) -> None:
    from .bot import WALLETS, StateStore

    venue = build_live(settings)
    st = StateStore(settings.state_dir / "state.json").data
    for w in WALLETS:
        print(f"Wallet {w} {venue.addr(w)}: {venue.usdc(w):.2f} USDC, "
              f"{venue.paper_raw(w) / SCALE:,.0f} PAPER non stakés, positions {[p.id for p in venue.positions(w)]}")
    print(f"Phase: {st['phase']}  Pause: {st.get('paused')}  Stratégie: {settings.params.strategy}"
          f"{' (un wallet)' if settings.params.one_wallet else ''}")
    c = st.get("cycle")
    if c:
        legs = ", ".join(f"{'long' if leg == 'A' else 'short'} #{c['legs'][leg]['pos_id']} @ {c['legs'][leg]['entry']}"
                         for leg in ("A", "B"))
        extra = (f", réarmement à -{c['close_move']:.2%}, encaissement à +{c['reset']:.2%}, "
                 f"{c.get('rearms', 0)} réarmement(s)" if c.get("mode") == "ladder" else f", fermeture à ±{c['close_move']:.2%}")
        print(f"Cycle #{c['n']} ({c.get('mode', 'pair')}): {legs}{extra}")
    for w in WALLETS:
        expiry = venue.key_expiry(w)
        print(f"Session key {w}: " + (f"expire dans {(expiry - time.time()) / 86400:.1f} jours" if expiry else "absente"))
    t = st["totals"]
    print(f"Cycles: {t['cycles']}  PAPER générés: {t['paper_minted']:,.0f}  coût: {t['usd_spent']:.2f}$  "
          f"(+{t.get('usd_offcycle', 0):.2f}$ hors cycle)  PAPER stakés par le bot: {t['paper_staked']:,.0f}")
    for c in st["history"][-10:]:
        print(f"  cycle: {c['usd']:.2f}$ -> {c['paper']:,.0f} PAPER")


def cmd_plan(settings, args) -> None:
    """Read-only: what the bot would do right now."""
    import math

    from .strategy import (LADDER_COST_WINDOW, MIN_CORE_TRANSFER_USD, MINT_FACTOR, Skip, choose_plan,
                           cost_per_1000_at, plan_rebalance, win_payout)

    venue = build_live(settings)
    p = settings.params
    bal = {w: venue.usdc(w) for w in ("A", "B")}
    mins = venue.minimums()
    emission = venue.paper_emission()
    try:
        cycle = json.loads((settings.state_dir / "state.json").read_text()).get("cycle")
    except (FileNotFoundError, ValueError):
        cycle = None
    if cycle:
        print(f"Cycle #{cycle['n']} en cours ({cycle.get('mode', 'pair')}, x{cycle['leverage']}): ce qui suit vaut pour "
              "le cycle suivant, avec la marge que celui-ci bloque encore (au gain ou à la perte près)")
        for w in bal:
            bal[w] += sum(pos.margin or 0 for pos in venue.positions(w))
    print(f"Soldes PaperTrade: A={bal['A']:.2f}$  B={bal['B']:.2f}$   minimums: marge {mins[0]:.2f}$, notionnel {mins[1]:,.0f}$")
    print(f"Émission: {f'~{emission:.2f} PAPER par $ perdu' if emission else 'illisible'}   stratégie: {p.strategy}"
          f"{', un seul wallet (A)' if p.one_wallet else ''}")
    if p.one_wallet:
        moved = float(math.floor(bal["B"]))
        if moved >= MIN_CORE_TRANSFER_USD:
            print(f"-> d'abord transférer {moved:.0f}$ de B vers A (tout se joue ensuite sur A)")
            bal = {"A": bal["A"] + moved - 1, "B": bal["B"] - moved}
    else:
        t = plan_rebalance(bal["A"], bal["B"], p, mins)
        if t:
            print(f"-> d'abord rééquilibrer: {t.amount:.2f}$ de {t.src} vers {t.dst}")
            half = (bal["A"] + bal["B"]) / 2
            bal = {"A": half, "B": half}
    plan, note = choose_plan(bal, *mins, emission, p)
    if note:
        print(f"-> {note}")
    if isinstance(plan, Skip):
        print(f"-> pas de cycle: {plan.reason}")
        return
    if plan.reset:
        step_loss = plan.notional * plan.close_move
        cost = plan.notional * (plan.reset - win_payout(plan.reset, p.coin))
        print(f"-> échelle: long + short de {plan.margin:.2f}$ x{plan.leverage} ({plan.notional:,.0f}$ de notionnel "
              f"chacun){' sur A' if p.one_wallet else ''}")
        print(f"   la jambe qui perd {plan.close_move:.2%} est rouverte et l'ancienne fermée (~{step_loss:.2f}$ de perte, "
              f"~{step_loss * MINT_FACTOR * (emission or 0):,.0f} PAPER par marche); liquidation à "
              f"{1 / plan.leverage - 0.0005:.2%}")
        print(f"   période terminée quand l'autre jambe gagne {plan.reset:.2%}: ~{plan.reset / plan.close_move:.0f} "
              f"marches, ~{plan.notional * plan.reset * MINT_FACTOR * (emission or 0):,.0f} PAPER pour ~{cost:.2f}$"
              + (f" (~{cost_per_1000_at(plan.reset, emission, p.coin):.2f}$/1000, plafond {p.max_cost_per_1000:.2f}$)"
                 if emission else ""))
    else:
        loss = plan.notional * plan.close_move
        cost = plan.notional * (plan.close_move - win_payout(plan.close_move, p.coin))
        print(f"-> paire: marge {plan.margin:.2f}$ x{plan.leverage} par jambe ({plan.notional:,.0f}$ de notionnel), "
              f"fermeture à ±{plan.close_move:.2%}: la jambe perdante perd ~{loss:.2f}$ "
              f"(~{loss * MINT_FACTOR * (emission or 0):,.0f} PAPER), coût net ~{cost:.2f}$"
              + (f" (~{cost_per_1000_at(plan.close_move, emission, p.coin):.2f}$/1000, plafond "
                 f"{p.max_cost_per_1000:.2f}$)" if emission else ""))
    if plan.reset:
        print(f"   Le coût réel de chaque période varie de ~±1$/1000 autour de ce chiffre (latence des ordres): le bot "
              f"s'arrête si la moyenne des {LADDER_COST_WINDOW} dernières périodes dépasse nettement le plafond.")
    else:
        print(f"   Le coût réel est mesuré après chaque cycle et comparé au plafond sur {p.cost_window} cycle(s).")


def cmd_register(settings, args) -> None:
    venue = build_live(settings)
    if venue.register_keys():
        print("Session keys enregistrées. Attendez ~5 minutes avant la mini-position de test ou `run`.")
    else:
        raise SystemExit("Enregistrement refusé ou sans réponse claire (voir data/bot.log). Sans réponse claire: "
                         "relancez `register` une fois. Si le message parle de signature, le format EIP-712 a changé; "
                         "s'il parle d'origine/cookie, le relayer refuse les clients hors site: n'allez pas plus loin.")


def straddle_passed(settings) -> bool:
    """The long + short test on wallet A passed (tools/preflight_tiny_trade.py --straddle)."""
    try:
        verdict = json.loads((settings.state_dir / "preflight" / "straddle_A.json").read_text()).get("verdict", [])
    except (FileNotFoundError, ValueError):
        return False
    return not any(v[0] == "FAIL" for v in verdict) and any(v[0] == "PASS" and "coexist" in v[1] for v in verdict)


def startup_problems(settings, venue) -> list[str]:
    """What must hold before the bot may trade real money (README, «Mise en route»)."""
    problems = []
    if not venue.keys_ready():
        problems.append("session keys absentes ou proches de l'expiration: `python -m ptbot register`, "
                        "puis attendez 5 minutes")
    # ONE_WALLET's long + short test is required later, by the bot itself, before its first one-wallet
    # cycle: B's USDC is on A by then, and a pair of the previous version can finish first.
    checks = [(w, "tiny_trade", "visible", "mini-position de test du wallet", "--wallet {w}")
              for w in (("A",) if settings.params.one_wallet else settings.wallets)]
    for w, name, key, what, flags in checks:
        path = settings.state_dir / "preflight" / f"{name}_{w}.json"
        try:
            verdict = json.loads(path.read_text()).get("verdict", [])
        except (FileNotFoundError, ValueError):
            problems.append(f"{what} {w} jamais fait: "
                            f"`python tools/preflight_tiny_trade.py {flags.format(w=w)} --i-understand-this-trades`")
            continue
        if any(v[0] == "FAIL" for v in verdict) or not any(v[0] == "PASS" and key in v[1] for v in verdict):
            problems.append(f"{what} {w} en échec ({path}): ne lancez pas le bot")
    for w, wallet in settings.wallets.items():
        if not wallet.deposit_address:
            problems.append(f"DEPOSIT_ADDRESS_{w} manquante dans .env")
            continue
        try:
            from_api = venue.api.deposit_address(wallet.address)
        except Exception as e:
            log.warning("Adresse de dépôt de %s non vérifiable maintenant (%s)", w, e)
            continue
        if from_api and from_api.lower() != wallet.deposit_address.lower():
            problems.append(f"DEPOSIT_ADDRESS_{w} ({wallet.deposit_address}) différente de celle de l'API ({from_api})")
        try:
            size = venue.evm.balances_return_size(wallet.address)
            if size != 32:
                problems.append(f"balances() renvoie {size} octets au lieu de 32: le solde libre serait mal lu")
        except Exception as e:
            problems.append(f"balances() illisible pour {w}: {e}")
        try:
            venue.queued(w)
        except Exception as e:
            problems.append(f"userQueuedTotal() illisible pour {w} (réseau ?): {e}")
    try:
        if not venue.queued_supported and venue.api.queue_total() > 0:
            problems.append("la file de profits est active mais userQueuedTotal() n'existe pas sur le contrat: "
                            "les profits en attente seraient comptés comme des pertes")
    except Exception as e:
        log.warning("File de profits non vérifiable maintenant (%s)", e)
    offsets = []
    for name, read in (("PaperTrade", venue.api.server_time), ("HyperEVM", venue.evm.block_time)):
        try:
            offsets.append((name, time.time() - read()))
        except Exception as e:
            log.warning("Heure de %s illisible (%s)", name, e)
    for name, off in offsets:
        if abs(off) > 10:
            problems.append(f"horloge décalée de {off:+.0f} s par rapport à {name}: activez la synchronisation NTP")
    return problems


def cmd_run(settings, args) -> None:
    from .bot import Bot, StateStore

    from .bot import IncompatibleState, Timing

    venue = build_live(settings)
    problems = startup_problems(settings, venue)
    if problems:
        print("Le bot ne démarre pas tant que ces points ne sont pas réglés:")
        for p in problems:
            print(f"  - {p}")
        if not args.force:
            raise SystemExit(1)
        print("--force: démarrage malgré tout, à vos risques")
    if not args.yes:
        print("Le bot va trader de vrais fonds sur PaperTrade avec les deux wallets du fichier .env.")
        answer = input("Tapez OUI pour continuer: ").strip().upper()
        if answer != "OUI":
            print(f"Réponse {answer!r} au lieu de OUI: rien n'est lancé (tapez OUI après la question, ou `run --yes`).")
            return
    store = StateStore(settings.state_dir / "state.json")
    p = settings.params
    log.info("Démarrage: A=%s, B=%s, %s, stratégie %s%s", venue.addr("A"), venue.addr("B"), p.coin,
             f"échelle (marche {p.ladder_step:.2%})" if p.strategy == "ladder"
             else f"paire x{p.leverage} (sortie {p.close_move:.2%})", ", un seul wallet (A)" if p.one_wallet else "")
    try:
        bot = Bot(venue, settings.params, store, Timing(open_deadline_s=settings.open_deadline_s))
    except IncompatibleState as e:
        raise SystemExit(str(e))
    bot.straddle_check = lambda: straddle_passed(settings)
    bot.run(stop_file=Path("STOP"))


def cmd_sim(settings, args) -> None:
    from .bot import Bot, StateStore, Timing
    from .sim import RandomWalk, SimVenue

    if args.live_prices:
        from .hyperliquid import HyperliquidInfo
        hl = HyperliquidInfo(settings.hl_url)
        price_fn = lambda now: hl.bbo_mid(settings.params.coin)  # noqa: E731
    else:
        price_fn = RandomWalk(vol_per_hour=args.vol / 100, seed=args.seed)
    venue = SimVenue(price_fn, start_usdc=args.usdc, emission=args.emission, min_notional=args.min_notional,
                     realtime=args.live_prices)
    sim_state = settings.state_dir / "sim_state.json"
    sim_state.unlink(missing_ok=True)  # fresh state every simulation
    store = StateStore(sim_state)
    bot = Bot(venue, settings.params, store, Timing(heartbeat_s=3600))
    steps = int(args.hours * 3600 / bot.t.poll_s)
    bot.run(max_steps=steps)
    t = store.data["totals"]
    staked = sum(a.staked_raw for a in venue.accounts.values()) / SCALE
    left = sum(a.usdc + sum(p.margin for p in a.positions.values()) for a in venue.accounts.values())
    print(f"\nSimulation {args.hours}h ({settings.params.strategy}"
          f"{', un wallet' if settings.params.one_wallet else ''}): {t['cycles']} cycles, "
          f"{t['paper_minted']:,.0f} PAPER générés ({t['paper_minted'] / args.hours * 24:,.0f}/jour), "
          f"{staked:,.0f} stakés, coût {t['usd_spent']:.2f}$ "
          f"({t['usd_spent'] / max(t['paper_minted'], 1e-9) * 1000:.2f}$/1000), liquidations {venue.liquidations}, "
          f"USDC restants (marge en position incluse) {left:.2f}$, pause: {store.data.get('paused')}")


def cmd_stake_now(settings, args) -> None:
    from .bot import StateStore

    st = StateStore(settings.state_dir / "state.json").data
    if (st.get("phase", "idle") != "idle" or st.get("cycle")) and not args.force:
        raise SystemExit("Un cycle est en cours: un stake maintenant fausserait le compte de PAPER du cycle. "
                         "Attendez la fin du cycle ou arrêtez le bot (fichier STOP), ou utilisez --force.")
    venue = build_live(settings)
    for w in settings.wallets:
        raw = venue.paper_raw(w)
        if raw > 0:
            venue.stake(w, raw)
            print(f"Wallet {w}: stake de {raw / SCALE:,.2f} PAPER envoyé")


def cmd_close_all(settings, args) -> None:
    state = settings.state_dir / "state.json"
    if state.exists() and time.time() - state.stat().st_mtime < 30 and not args.force:
        raise SystemExit("Le bot semble tourner (state.json modifié il y a moins de 30 s) et rouvrirait une paire: "
                         "créez le fichier STOP, attendez qu'il s'arrête, puis relancez close-all (ou --force).")
    venue = build_live(settings)
    for w in settings.wallets:
        try:
            ids = [p.id for p in venue.positions(w)]
        except Exception as e:
            print(f"Wallet {w}: positions illisibles ({e}): fermez-les sur le site")
            continue
        for i in range(0, len(ids), 25):  # 25 positions max per close
            try:
                venue.close(w, ids[i:i + 25])
            except Exception as e:
                print(f"Wallet {w}: fermeture de {ids[i:i + 25]} refusée ({e}): fermez-les sur le site")
        print(f"Wallet {w}: fermeture demandée pour {ids}")


def cmd_close_cycle(settings, args) -> None:
    """Closes the current cycle the way the bot would (cancels pending orders, closes both legs,
    books the cost and PAPER), then stops without opening another one."""
    from .bot import Bot, StateStore, Timing

    state = settings.state_dir / "state.json"
    if state.exists() and time.time() - state.stat().st_mtime < 30 and not args.force:
        raise SystemExit("Le bot semble tourner (state.json modifié il y a moins de 30 s): créez le fichier STOP, "
                         "attendez qu'il s'arrête, puis relancez close-cycle (ou --force).")
    store = StateStore(state)
    bot = Bot(build_live(settings), settings.params, store, Timing(open_deadline_s=settings.open_deadline_s))
    phase = bot.st["phase"]
    if phase == "idle":
        print("Aucun cycle en cours.")
        return
    if phase == "rebalancing":
        raise SystemExit("Un transfert entre wallets est en cours: laissez le bot le terminer (`run`), puis recommencez.")
    if phase != "closing":
        bot._end_period(bot.v.now(), "fermeture demandée (close-cycle)")
        store.save()
    bot.run(stop_when_idle=True)
    t = bot.st["totals"]
    print(f"Cycle fermé et compté. Total: {t['cycles']} cycles, {t['paper_minted']:,.0f} PAPER pour {t['usd_spent']:.2f}$")


def cmd_resume(settings, args) -> None:
    from .bot import StateStore, apply_resume

    # A running bot rewrites state.json every step: it picks the request up from this file instead.
    (settings.state_dir / "state.resume").write_text("reset-costs" if args.reset_costs else "")
    store = StateStore(settings.state_dir / "state.json")
    apply_resume(store.data, args.reset_costs)
    store.save()
    print("Pause levée (prise en compte au prochain tour si le bot tourne).")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="ptbot", description="Bot delta neutral PaperTrade (2 wallets)")
    ap.add_argument("--env", default=".env")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="lecture seule: vérifie API, soldes, positions, domaines EIP-712")
    sub.add_parser("status", help="soldes, positions, statistiques du bot")
    sub.add_parser("plan", help="lecture seule: ce que le bot ferait maintenant")
    sub.add_parser("register", help="enregistre une nouvelle session key pour chaque wallet")
    r = sub.add_parser("run", help="lance le bot (fonds réels)")
    r.add_argument("--yes", action="store_true", help="pas de confirmation (pour nohup/systemd)")
    r.add_argument("--force", action="store_true", help="démarre même si les contrôles de sécurité échouent")
    s = sub.add_parser("sim", help="simulation hors-ligne de la stratégie")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--usdc", type=float, default=100, help="USDC de départ par wallet")
    s.add_argument("--emission", type=float, default=18.9, help="PAPER par $ perdu")
    s.add_argument("--min-notional", type=float, default=1000, help="notionnel minimum d'une position")
    s.add_argument("--vol", type=float, default=0.6, help="volatilité horaire en %%")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--live-prices", action="store_true", help="prix BBO Hyperliquid réels, temps réel")
    sn = sub.add_parser("stake-now", help="stake tout le PAPER non staké des deux wallets")
    sn.add_argument("--force", action="store_true", help="même si un cycle est en cours")
    ca = sub.add_parser("close-all", help="ferme toutes les positions des deux wallets")
    ca.add_argument("--force", action="store_true", help="même si le bot semble tourner")
    cc = sub.add_parser("close-cycle", help="ferme le cycle en cours (bilan compris) puis s'arrête")
    cc.add_argument("--force", action="store_true", help="même si le bot semble tourner")
    rs = sub.add_parser("resume", help="lève la pause")
    rs.add_argument("--reset-costs", action="store_true", help="oublie l'historique de coût (plafond)")
    args = ap.parse_args(argv)

    from .config import load_settings
    settings = load_settings(args.env, need_keys=args.cmd != "sim")
    setup_logging(settings.state_dir, args.verbose)
    {"probe": cmd_probe, "status": cmd_status, "plan": cmd_plan, "register": cmd_register, "run": cmd_run,
     "sim": cmd_sim, "stake-now": cmd_stake_now, "close-all": cmd_close_all, "close-cycle": cmd_close_cycle,
     "resume": cmd_resume}[args.cmd](settings, args)


if __name__ == "__main__":
    main()
