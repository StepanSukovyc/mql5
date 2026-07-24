"""
Liquidity Sweep Scalping Strategy
==================================
Forex skalpovací strategie založená na false breakout / liquidity sweep logice.

ARCHITEKTURA:
--------------
Třída LiquiditySweepScalpingStrategy pracuje výhradně přes ScalpingAppContext,
který obaluje existující interní služby (MT5 data, trade execution, account info).
Konfigurace probíhá přes proměnné prostředí s prefixem SCALP_. Strategie se
aktivuje jako poslední záloha po vyčerpání celého fallback řetězce (Cloud Ollama
-> Primary -> Parallel -> Reversal -> Quant) a pouze pokud volná marže účtu
překračuje SCALP_ACTIVATION_MARGIN_PERCENT (výchozí 5 %).

ZÁSADNÍ OBCHODNÍ KOMENTÁŘE:
-----------------------------
Proč samotné svíčkové formace nestačí:
  Svíčkové patterny (engulfing, pin bar) mají bez kontextu statisticky nízkou
  pravděpodobnost úspěchu. Potřebujeme potvrzení odmítnutí ceny více způsoby:
  wick + engulfing + EMA kontext + potvrzení sweep lokálního extrému.

Proč je spread kritický u skalpování:
  Cíl zisku je malý (desítky pipů). Vysoký spread okamžitě eliminuje celý
  potenciální zisk nebo způsobí ztrátu hned po vstupu. Proto má každý symbol
  vlastní limit max spreadu a strategie neotvírá obchod při příliš vysokém spreadu.

Proč je nutný session filter:
  Mimo hlavní obchodní seance (London, New York) je likvidita nízká, spreads jsou
  vyšší a false breaky jsou mnohem častější. Vstupovat do obchodu mimo likvidní
  okna zvyšuje riziko a snižuje pravděpodobnost profitabilního setup.

Proč je nebezpečný martingale a grid:
  Martingale exponenciálně zvyšuje expozici po každé ztrátě – jedno neočekávané
  hnutí trhu může vymazat celý účet. Grid přidává pozice bez ohledu na tržní
  kontext a vytváří nekontrolovatelnou expozici při trendovém pohybu.

Proč strategie bez klasického SL musí mít kvalitní cleanup:
  Bez pevného stop lossu může pozice držet záporný float neomezeně. Proto jsou
  implementovány: max_floating_loss (absolutní USD limit), max_holding_time
  (časový limit v minutách), emergency_stop_reference (krajní záchranná úroveň)
  a invalidation_level (logické zneplatnění setupu). Tyto hodnoty jsou uloženy
  v state souboru a vyhodnocovány při každém volání manage_existing_positions().

Proč se musí testovat každý symbol samostatně i celé portfolio:
  Forex páry jsou korelované (EURUSD a GBPUSD se pohybují podobně). Při systémové
  události (např. ECB oznámení) mohou selhat všechny otevřené pozice současně.
  Proto jsou implementovány: max_positions_total, max_usd_exposure a max_daily_loss
  jako portfolio limity nad rámec per-symbol limitů.

INTEGRACE:
-----------
final_decision.py volá:
  1. manage_scalping_positions(service_folder) – vždy (správa otevřených pozic)
  2. run_scalping_strategy(service_folder, account_state, open_positions) – jako fallback

Příklad periodického volání:
  ctx = ScalpingAppContext(service_folder=service_folder)
  strategy = LiquiditySweepScalpingStrategy(ctx)
  if strategy.run():
      print("Scalping trade opened")

Očekávané metody interních služeb (rozhraní AppContext):
  ctx.get_config()            -> Dict[str, Any]
  ctx.get_account_state()     -> Dict[str, Any]
  ctx.get_open_positions()    -> List[Dict]
  ctx.get_ohlcv(sym, tf, n)   -> Optional[np.ndarray]
  ctx.get_spread(symbol)      -> Optional[float]
  ctx.open_trade(...)         -> bool
  ctx.close_position(...)     -> bool
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import MetaTrader5 as mt5

from env_utils import get_bool_env, get_float_env, get_int_env, parse_csv_env
from strategy_context import (
	StrategyContext,
	build_strategy_comment,
	count_open_positions_for_strategy,
	position_belongs_to_strategy,
)
from trade_execution import close_position_by_ticket, execute_trade


# ─────────────────────────────────────────────────────────────────────────────
# Konstanty
# ─────────────────────────────────────────────────────────────────────────────

SCALP_DEFAULT_STRATEGY_ID = "liquidity_sweep_scalping"
SCALP_DEFAULT_MAGIC = 234600

# Bodové váhy pro scoring (součet při plném shodě = 105)
_SCORE_LIQUIDITY_SWEEP = 40.0   # Klíčová podmínka – sweep lokálního extrému
_SCORE_ENGULFING = 20.0         # Potvrzení engulfing patternem
_SCORE_STRONG_WICK = 15.0       # Potvrzení silným knotem
_SCORE_EMA_CONTEXT = 15.0       # EMA kontext (trend alignment)
_SCORE_ATR_FILTER = 5.0         # Volatilita v povoleném rozsahu
_SCORE_SPREAD_FILTER = 5.0      # Spread pod limitem
_SCORE_SESSION_FILTER = 5.0     # Obchodní seance aktivní


# ─────────────────────────────────────────────────────────────────────────────
# Datové třídy
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StrategySignal:
	"""
	Výsledek vyhodnocení vstupní podmínky pro jeden symbol.

	Obsahuje veškerá data pro rozhodnutí o vstupu i pro
	následnou správu pozice (invalidation, emergency stop).
	"""
	symbol: str
	side: str           # "BUY" | "SELL" | "HOLD"
	score: float        # 0.0 – 105.0
	reason: str         # Strojový kód důvodu
	invalidation_level: float    # Cenová úroveň zneplatňující setup
	take_profit: float           # Cílová úroveň zisku (v ceně)
	emergency_stop_reference: Optional[float] = None
	metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExitDecision:
	"""
	Rozhodnutí o uzavření otevřené pozice.

	urgency='emergency' = uzavřít okamžitě bez ohledu na časové filtry.
	"""
	should_close: bool
	reason: str
	urgency: str = "normal"   # "normal" | "emergency"
	metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SignalContext:
	"""
	Mezivýsledky hodnocení kvality signálu – podklady pro scoring.

	Každé pole odpovídá jedné splněné nebo nesplněné podmínce.
	"""
	liquidity_sweep_detected: bool = False
	engulfing_detected: bool = False
	strong_wick_detected: bool = False
	ema_context_ok: bool = False
	atr_ok: bool = False
	spread_ok: bool = False
	session_ok: bool = False
	position_ok: bool = False
	score_details: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class _TradingSession:
	"""Obchodní seance definovaná v UTC hodinách a minutách."""
	name: str
	start_hour: int
	start_minute: int
	end_hour: int
	end_minute: int


# ─────────────────────────────────────────────────────────────────────────────
# Načtení konfigurace z env proměnných
# ─────────────────────────────────────────────────────────────────────────────

def _parse_session_env(name: str, sh: int, sm: int, eh: int, em: int) -> _TradingSession:
	return _TradingSession(
		name=name,
		start_hour=get_int_env(f"SCALP_SESSION_{name}_START_HOUR_UTC", sh),
		start_minute=get_int_env(f"SCALP_SESSION_{name}_START_MINUTE_UTC", sm),
		end_hour=get_int_env(f"SCALP_SESSION_{name}_END_HOUR_UTC", eh),
		end_minute=get_int_env(f"SCALP_SESSION_{name}_END_MINUTE_UTC", em),
	)


def _load_scalping_config() -> Dict[str, Any]:
	"""
	Načte konfiguraci skalpovací strategie z proměnných prostředí.

	Každé volání znovu čte env, takže změny v .env se projeví bez restartu
	(pokud je .env načítán na začátku smyčky). Symbol-specifická nastavení
	používají vzor SCALP_MAX_SPREAD_{SYMBOL} (bez suffixu brokera).
	"""
	suffix = os.getenv("MT5_SYMBOL_SUFFIX", "")
	raw_syms = parse_csv_env("SCALP_SYMBOLS", "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,NZDUSD")
	symbols = [s if s.endswith(suffix) else f"{s}{suffix}" for s in raw_syms]

	tf_name = os.getenv("SCALP_TIMEFRAME", "M5").upper().strip()
	_tf_map = {
		"M1": mt5.TIMEFRAME_M1,
		"M5": mt5.TIMEFRAME_M5,
		"M15": mt5.TIMEFRAME_M15,
		"M30": mt5.TIMEFRAME_M30,
		"H1": mt5.TIMEFRAME_H1,
	}
	timeframe = _tf_map.get(tf_name, mt5.TIMEFRAME_M5)
	tf_minutes = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}.get(tf_name, 5)

	sessions: List[_TradingSession] = []
	if get_bool_env("SCALP_SESSION_LONDON_ENABLED", True):
		sessions.append(_parse_session_env("LONDON", 7, 0, 11, 0))
	if get_bool_env("SCALP_SESSION_NEWYORK_ENABLED", True):
		sessions.append(_parse_session_env("NEWYORK", 13, 30, 17, 0))

	def _spread(sym: str, default: float) -> float:
		return get_float_env(f"SCALP_MAX_SPREAD_{sym}", default)

	def _min_atr(sym: str, d: float) -> float:
		return get_float_env(f"SCALP_MIN_ATR_{sym}", d)

	def _max_atr(sym: str, d: float) -> float:
		return get_float_env(f"SCALP_MAX_ATR_{sym}", d)

	def _tp(sym: str, d: float) -> float:
		return get_float_env(f"SCALP_FIXED_TP_{sym}", d)

	return {
		"strategy_id": os.getenv("SCALP_STRATEGY_ID", SCALP_DEFAULT_STRATEGY_ID),
		"magic": get_int_env("SCALP_STRATEGY_MAGIC", SCALP_DEFAULT_MAGIC),
		"enabled": get_bool_env("SCALP_ENABLED", True),
		"symbols": symbols,
		"raw_symbols": raw_syms,
		"timeframe": timeframe,
		"timeframe_name": tf_name,
		"tf_minutes": tf_minutes,
		"lookback_bars": max(get_int_env("SCALP_LOOKBACK_BARS", 20), 5),
		"min_signal_score": get_float_env("SCALP_MIN_SIGNAL_SCORE", 70.0),
		"ema_fast_period": max(get_int_env("SCALP_EMA_FAST_PERIOD", 50), 5),
		"ema_slow_period": max(get_int_env("SCALP_EMA_SLOW_PERIOD", 200), 10),
		"use_ema_filter": get_bool_env("SCALP_USE_EMA_FILTER", True),
		"atr_period": max(get_int_env("SCALP_ATR_PERIOD", 14), 2),
		"use_session_filter": get_bool_env("SCALP_USE_SESSION_FILTER", True),
		"sessions": sessions,
		"fixed_volume": max(get_float_env("SCALP_FIXED_VOLUME", 0.01), 0.001),
		"use_risk_based_volume": get_bool_env("SCALP_USE_RISK_BASED_VOLUME", False),
		"risk_percent": get_float_env("SCALP_RISK_PERCENT", 0.5),
		"tp_mode": os.getenv("SCALP_TP_MODE", "atr").lower().strip(),
		"tp_atr_multiplier": get_float_env("SCALP_TP_ATR_MULTIPLIER", 0.5),
		"max_holding_bars": max(get_int_env("SCALP_MAX_HOLDING_BARS", 8), 1),
		"max_positions_total": max(get_int_env("SCALP_MAX_POSITIONS_TOTAL", 3), 0),
		"max_positions_per_symbol": max(get_int_env("SCALP_MAX_POSITIONS_PER_SYMBOL", 1), 1),
		"max_usd_exposure": get_float_env("SCALP_MAX_USD_EXPOSURE", 2.0),
		"max_floating_loss_per_trade": get_float_env("SCALP_MAX_FLOATING_LOSS_PER_TRADE", 20.0),
		"max_daily_loss": get_float_env("SCALP_MAX_DAILY_LOSS", 100.0),
		"use_emergency_stop": get_bool_env("SCALP_USE_EMERGENCY_STOP_REFERENCE", True),
		"allow_buy": get_bool_env("SCALP_ALLOW_BUY", True),
		"allow_sell": get_bool_env("SCALP_ALLOW_SELL", True),
		"activation_margin_percent": get_float_env("SCALP_ACTIVATION_MARGIN_PERCENT", 5.0),
		# Drawdown check slouží jen jako emergency backstop pro exit existujících pozic.
		# Vstup do obchodu je řízený výlučně prahovou hodnotou volné marže (activation_margin_percent).
		"max_account_drawdown_percent": get_float_env("SCALP_MAX_ACCOUNT_DRAWDOWN_PERCENT", 90.0),
		"friday_cutoff_hour_utc": get_int_env("SCALP_FRIDAY_CUTOFF_HOUR_UTC", 16),
		"max_spread_by_symbol": {
			"EURUSD": _spread("EURUSD", 15.0),
			"GBPUSD": _spread("GBPUSD", 25.0),
			"USDJPY": _spread("USDJPY", 20.0),
			"AUDUSD": _spread("AUDUSD", 25.0),
			"USDCAD": _spread("USDCAD", 30.0),
			"NZDUSD": _spread("NZDUSD", 30.0),
		},
		"default_max_spread": get_float_env("SCALP_MAX_SPREAD_DEFAULT", 30.0),
		"min_atr_by_symbol": {
			"EURUSD": _min_atr("EURUSD", 0.0003),
			"GBPUSD": _min_atr("GBPUSD", 0.0005),
			"USDJPY": _min_atr("USDJPY", 0.03),
			"AUDUSD": _min_atr("AUDUSD", 0.0003),
			"USDCAD": _min_atr("USDCAD", 0.0004),
			"NZDUSD": _min_atr("NZDUSD", 0.0003),
		},
		"max_atr_by_symbol": {
			"EURUSD": _max_atr("EURUSD", 0.0020),
			"GBPUSD": _max_atr("GBPUSD", 0.0030),
			"USDJPY": _max_atr("USDJPY", 0.20),
			"AUDUSD": _max_atr("AUDUSD", 0.0020),
			"USDCAD": _max_atr("USDCAD", 0.0025),
			"NZDUSD": _max_atr("NZDUSD", 0.0020),
		},
		"fixed_tp_by_symbol": {
			"EURUSD": _tp("EURUSD", 50.0),
			"GBPUSD": _tp("GBPUSD", 70.0),
			"USDJPY": _tp("USDJPY", 60.0),
			"AUDUSD": _tp("AUDUSD", 50.0),
			"USDCAD": _tp("USDCAD", 55.0),
			"NZDUSD": _tp("NZDUSD", 50.0),
		},
	}


# ─────────────────────────────────────────────────────────────────────────────
# MT5 pomocné funkce (nízká úroveň – nejsou součástí třídy strategie)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_ohlcv(symbol: str, timeframe: int, count: int) -> Optional[np.ndarray]:
	"""
	Načte OHLCV data z MT5 výhradně pro uzavřené svíčky.

	start_pos=1 přeskočí aktuálně formující se svíčku (pozice 0).
	Vrácené pole je seřazeno od nejstarší po nejnovější;
	rates[-1] je vždy poslední UZAVŘENÁ svíčka – žádný lookahead bias.

	Pole výsledného numpy structured array:
	    time, open, high, low, close, tick_volume, spread, real_volume
	"""
	if count < 1:
		return None
	rates = mt5.copy_rates_from_pos(symbol, timeframe, 1, count)
	if rates is None or len(rates) == 0:
		return None
	return rates


def _get_spread_points(symbol: str) -> Optional[float]:
	"""Vrátí aktuální spread v bodech (points) pro daný symbol."""
	tick = mt5.symbol_info_tick(symbol)
	info = mt5.symbol_info(symbol)
	if tick is None or info is None or info.point <= 0:
		return None
	return float((tick.ask - tick.bid) / info.point)


def _get_point_size(symbol: str) -> float:
	"""Vrátí velikost 1 bodu (point) pro symbol, výchozí 0.00001."""
	info = mt5.symbol_info(symbol)
	if info is None or info.point <= 0:
		return 0.00001
	return float(info.point)


def _strip_suffix(symbol: str) -> str:
	"""Odstraní suffix brokera ze symbolu pro lookup v konfiguraci.

	Nejprve použije MT5_SYMBOL_SUFFIX z env. Pokud není nastaven,
	automaticky detekuje běžné broker suffixes (_ecn, _raw, _pro …).
	Tím je zajištěno, že EURUSD_ecn → EURUSD funguje i bez MT5_SYMBOL_SUFFIX.
	"""
	suffix = os.getenv("MT5_SYMBOL_SUFFIX", "")
	if suffix:
		if symbol.endswith(suffix):
			return symbol[: -len(suffix)]
		return symbol
	# MT5_SYMBOL_SUFFIX není nastaveno – zkusíme běžné suffixes automaticky
	for _sfx in ("_ecn", "_raw", "_pro", "_std", ".ecn", ".raw"):
		if symbol.endswith(_sfx):
			return symbol[: -len(_sfx)]
	return symbol


# ─────────────────────────────────────────────────────────────────────────────
# AppContext – kontejner pro dependency injection
# ─────────────────────────────────────────────────────────────────────────────

class ScalpingAppContext:
	"""
	Lehký DI kontejner obalující existující interní služby.

	Každá metoda deleguje na konkrétní interní funkci. Pro testování
	lze ScalpingAppContext nahradit mockem s identickým rozhraním.

	Rozhraní (pro dokumentaci):
	    get_config()               -> Dict[str, Any]
	    get_account_state()        -> Dict[str, Any]
	    get_open_positions()       -> List[Dict]
	    get_ohlcv(sym, tf, count)  -> Optional[np.ndarray]
	    get_spread(symbol)         -> Optional[float]
	    open_trade(...)            -> bool
	    close_position(...)        -> bool
	"""

	def __init__(
		self,
		*,
		logger: Optional[logging.Logger] = None,
		service_folder: Optional[Path] = None,
	) -> None:
		if logger is not None:
			self.logger = logger
		else:
			# Konfigurujeme vlastní handler, aby [SCALP:...] zprávy byly vidět v terminálu
			# stejně jako print() volání v ostatních modulech.
			_log = logging.getLogger("liquidity_sweep_scalping")
			if not _log.handlers:
				_h = logging.StreamHandler()
				_h.setFormatter(logging.Formatter("%(message)s"))
				_log.addHandler(_h)
				_log.setLevel(logging.INFO)
			_log.propagate = False
			self.logger = _log
		self.service_folder = service_folder

	def get_config(self) -> Dict[str, Any]:
		return _load_scalping_config()

	def get_account_state(self) -> Dict[str, Any]:
		from account_state import get_account_state
		return get_account_state(include_margin_percent=True)

	def get_open_positions(self) -> List[Dict]:
		from mt5_positions import get_open_positions
		return get_open_positions()

	def get_ohlcv(self, symbol: str, timeframe: int, count: int) -> Optional[np.ndarray]:
		return _fetch_ohlcv(symbol, timeframe, count)

	def get_spread(self, symbol: str) -> Optional[float]:
		return _get_spread_points(symbol)

	def open_trade(
		self,
		symbol: str,
		action: str,
		lot_size: float,
		*,
		take_profit: Optional[float] = None,
		strategy_id: str = SCALP_DEFAULT_STRATEGY_ID,
		magic: int = SCALP_DEFAULT_MAGIC,
		comment: Optional[str] = None,
	) -> bool:
		"""Otevře obchod přes interní trade execution service."""
		lot_src = (
			"scalping_risk" if get_bool_env("SCALP_USE_RISK_BASED_VOLUME", False)
			else "scalping_fixed"
		)
		return execute_trade(
			symbol=symbol,
			action=action,
			lot_size=lot_size,
			service_folder=self.service_folder,
			take_profit=take_profit,
			lot_source=lot_src,
			strategy_id=strategy_id,
			magic=magic,
			comment=comment,
		)

	def close_position(
		self,
		ticket: int,
		symbol: str,
		position_type: int,
		volume: float,
		*,
		magic: int = SCALP_DEFAULT_MAGIC,
		comment: str = "scalp_close",
	) -> bool:
		"""Uzavře existující pozici přes interní trade execution service."""
		return close_position_by_ticket(
			position_ticket=ticket,
			symbol=symbol,
			position_type=position_type,
			volume=volume,
			comment=comment,
			magic=magic,
		)


# ─────────────────────────────────────────────────────────────────────────────
# Hlavní třída strategie
# ─────────────────────────────────────────────────────────────────────────────

class LiquiditySweepScalpingStrategy:
	"""
	Forex skalpovací strategie: Liquidity Sweep / False Breakout.

	Životní cyklus:
	  1. run() – volán periodicky jako fallback po ostatních strategiích.
	  2. manage_existing_positions() – volán vždy, nezávisle na run().
	  3. process_symbol(symbol) – vyhodnotí jeden symbol a případně otevře obchod.

	Bezpečnostní záruky:
	  - Žádný martingale, žádný grid.
	  - Maximálně 1 pozice na symbol, SCALP_MAX_POSITIONS_TOTAL celkem.
	  - Ochrana opakovaného vstupu: _last_processed_bar_time[symbol].
	  - State soubor scalping_position_state.json uchovává metadata pozic.
	"""

	def __init__(self, app_context: ScalpingAppContext) -> None:
		self._ctx = app_context
		self._log = app_context.logger
		self._cfg: Dict[str, Any] = {}
		# Ochrana duplicitního vstupu: symbol -> Unix timestamp zpracované svíčky
		self._last_processed_bar_time: Dict[str, int] = {}
		# Metadata pozic: str(ticket) -> dict
		self._position_state: Dict[str, Dict[str, Any]] = {}
		self._state_file: Optional[Path] = (
			app_context.service_folder / "trade_logs" / "scalping_position_state.json"
			if app_context.service_folder is not None
			else None
		)
		self._load_position_state()

	# ── Hlavní vstupní body ───────────────────────────────────────────────────

	def run(self) -> bool:
		"""
		Hlavní vstupní bod strategie.

		Vždy spravuje existující pozice (manage_existing_positions).
		Nové obchody hledá pouze pokud projde aktivační podmínka.

		Vrací True pokud byl otevřen alespoň jeden nový obchod.
		"""
		try:
			self._cfg = self._ctx.get_config()
		except Exception as exc:
			self._log.error(f"[SCALP] Nelze načíst konfiguraci: {exc}")
			return False

		if not self._cfg.get("enabled", True):
			self._log.debug("[SCALP] Strategie je vypnutá (SCALP_ENABLED=false)")
			return False

		try:
			account_state = self._ctx.get_account_state()
			open_positions = self._ctx.get_open_positions()
		except Exception as exc:
			self._log.error(f"[SCALP] Nelze načíst stav účtu nebo pozic: {exc}")
			return False

		# Vždy spravujeme existující pozice strategie
		self.manage_existing_positions()

		# Nový obchod jen pokud projde aktivační podmínka
		if not self._activation_gate_passed(account_state, open_positions):
			return False

		print(f"[SCALP] Vyhodnocuji {len(self._cfg.get('symbols', []))} symbolů...")
		trade_opened = False
		hold_reasons: list = []
		for symbol in self._cfg.get("symbols", []):
			try:
				if self.process_symbol(symbol):
					trade_opened = True
					break  # Skalpovací strategie otevírá max 1 obchod za cyklus
				else:
					# Sbíráme důvody pro souhrnný výstup
					hold_reasons.append(symbol)
			except Exception as exc:
				self._log.error(f"[SCALP] Chyba při zpracování {symbol}: {exc}")
				continue

		if not trade_opened and hold_reasons:
			print(f"[SCALP] Žádný signal – všechny symboly HOLD ({', '.join(hold_reasons)})")

		return trade_opened

	def process_symbol(self, symbol: str) -> bool:
		"""
		Zpracuje jeden symbol: načte OHLCV, vyhodnotí signál, případně otevře obchod.

		Ochrana proti duplicitnímu vstupu:
		  - Ukládáme Unix timestamp poslední zpracované uzavřené svíčky.
		  - Pokud je timestamp stejný jako při posledním volání, přeskočíme symbol.
		  - Nová svíčka = nový timestamp -> zpracujeme.

		Vrací True pokud byl otevřen obchod.
		"""
		cfg = self._cfg

		try:
			account_state = self._ctx.get_account_state()
			open_positions = self._ctx.get_open_positions()
		except Exception as exc:
			self._log.warning(f"[SCALP:{symbol}] Nelze načíst stav: {exc}")
			return False

		# Kolik svíček potřebujeme: EMA200 + reserve
		ema_slow = cfg.get("ema_slow_period", 200)
		lookback = cfg.get("lookback_bars", 20)
		atr_period = cfg.get("atr_period", 14)
		total_needed = max(ema_slow + 10, lookback + atr_period + 10)

		rates = self._ctx.get_ohlcv(symbol, cfg["timeframe"], total_needed)
		min_bars = max(lookback + atr_period + 2, 20)
		if rates is None or len(rates) < min_bars:
			self._log.debug(
				f"[SCALP:{symbol}] Nedostatek dat "
				f"({0 if rates is None else len(rates)}/{min_bars} svíček)"
			)
			return False

		# Ochrana duplicitního vstupu – porovnání timestampu poslední svíčky
		latest_bar_time = int(rates[-1]["time"])
		if self._last_processed_bar_time.get(symbol) == latest_bar_time:
			self._log.debug(f"[SCALP:{symbol}] Svíčka {latest_bar_time} již zpracována")
			return False

		spread = self._ctx.get_spread(symbol)
		if spread is None:
			self._log.debug(f"[SCALP:{symbol}] Nelze zjistit spread")
			return False

		signal = self.evaluate_entry_signal(rates, symbol, spread, account_state, open_positions)

		# Zaznamenáme zpracování svíčky bez ohledu na výsledek signálu
		self._last_processed_bar_time[symbol] = latest_bar_time

		if signal.side == "HOLD":
			self._log.info(
				f"[SCALP:{symbol}] HOLD – {signal.reason} "
				f"(long={signal.metadata.get('long_score', 0):.0f} short={signal.metadata.get('short_score', 0):.0f})"
			)
			return False

		return self.open_trade_if_allowed(signal, symbol, account_state, open_positions)

	def manage_existing_positions(self) -> None:
		"""
		Vyhodnotí existující pozice patřící této strategii a uzavře je pokud jsou splněny výstupní podmínky.

		Volá se vždy při každém cyklu, nezávisle na tom, zda jiné strategie obchodovaly.
		Tím je zajištěno, že otevřené skalpovací pozice jsou průběžně chráněny.
		"""
		cfg = self._cfg or _load_scalping_config()
		strategy_ctx = self._build_strategy_context(cfg)

		try:
			open_positions = self._ctx.get_open_positions()
			account_state = self._ctx.get_account_state()
		except Exception as exc:
			self._log.error(f"[SCALP] manage_existing_positions – nelze načíst data: {exc}")
			return

		my_positions = [
			pos for pos in open_positions
			if position_belongs_to_strategy(pos, strategy_ctx)
		]

		self._cleanup_stale_state(open_positions)

		if not my_positions:
			return

		self._log.info(f"[SCALP] Správa {len(my_positions)} otevřených pozic strategie")

		for position in my_positions:
			symbol = position.get("symbol", "")
			ticket = position.get("ticket", 0)

			bars_needed = cfg.get("lookback_bars", 20) + cfg.get("atr_period", 14) + 5
			rates = self._ctx.get_ohlcv(symbol, cfg.get("timeframe", mt5.TIMEFRAME_M5), bars_needed)
			spread = float(self._ctx.get_spread(symbol) or 0.0)

			try:
				exit_decision = self.evaluate_exit_conditions(position, rates, spread, account_state)
			except Exception as exc:
				self._log.error(f"[SCALP:{symbol}] Chyba při vyhodnocení exitu pro ticket {ticket}: {exc}")
				continue

			if exit_decision.should_close:
				self._log.info(
					f"[SCALP:{symbol}] EXIT pro ticket {ticket}: "
					f"{exit_decision.reason} (urgency={exit_decision.urgency})"
				)
				self.close_position_if_required(position, exit_decision)

	# ── Vstupní logika ────────────────────────────────────────────────────────

	def evaluate_entry_signal(
		self,
		rates: np.ndarray,
		symbol: str,
		spread: float,
		account_state: Dict[str, Any],
		open_positions: List[Dict],
	) -> StrategySignal:
		"""
		Vyhodnotí vstupní podmínky pro symbol a vrátí StrategySignal.

		Algoritmus:
		1. Vypočte ATR a EMA z uzavřených svíček (žádný lookahead).
		2. Detekuje liquidity sweep (false breakout lokálního extrému).
		3. Ověří potvrzující signály (wick, engulfing, EMA kontext).
		4. Aplikuje filtry (spread, ATR range, session, position limit).
		5. Vypočte scoring pro LONG a SHORT setup.
		6. Pokud score >= min_signal_score, vrátí BUY nebo SELL.
		7. Jinak vrátí HOLD s kódem důvodu.
		"""
		cfg = self._cfg
		n = len(rates)
		if n < 3:
			return self._hold(symbol, "insufficient_bars", 0.0, 0.0)

		closes = rates["close"].astype(float)
		highs = rates["high"].astype(float)
		lows = rates["low"].astype(float)
		opens = rates["open"].astype(float)

		atr_period = cfg.get("atr_period", 14)
		ema_fast_p = cfg.get("ema_fast_period", 50)
		ema_slow_p = cfg.get("ema_slow_period", 200)
		lookback = cfg.get("lookback_bars", 20)

		# ── Technické indikátory ─────────────────────────────────────────────
		atr = self.calculate_atr(rates, atr_period)
		if atr is None or atr <= 0.0:
			return self._hold(symbol, "atr_calculation_failed", 0.0, 0.0)

		ema_fast = self.calculate_ema(closes, ema_fast_p)
		ema_slow_arr = self.calculate_ema(closes, ema_slow_p)

		# ── Poslední dvě uzavřené svíčky ─────────────────────────────────────
		last_idx = n - 1
		prev_idx = n - 2

		def _candle(i: int) -> Dict[str, float]:
			return {
				"time": int(rates[i]["time"]),
				"open": float(opens[i]),
				"high": float(highs[i]),
				"low": float(lows[i]),
				"close": float(closes[i]),
			}

		last_c = _candle(last_idx)
		prev_c = _candle(prev_idx)

		# ── Lokální extrémy za lookback okno (bez poslední svíčky) ────────────
		win_start = max(0, last_idx - lookback)
		w_lows = lows[win_start:last_idx]
		w_highs = highs[win_start:last_idx]

		if len(w_lows) == 0:
			return self._hold(symbol, "empty_lookback_window", 0.0, 0.0)

		local_low = float(np.min(w_lows))
		local_high = float(np.max(w_highs))

		# ── EMA kontext ───────────────────────────────────────────────────────
		cur_ema_fast = float(ema_fast[-1]) if ema_fast is not None and len(ema_fast) > 0 else None
		cur_ema_slow = float(ema_slow_arr[-1]) if ema_slow_arr is not None and len(ema_slow_arr) > 0 else None

		use_ema = cfg.get("use_ema_filter", True)
		if use_ema and cur_ema_fast is not None:
			ema_ok_long = last_c["close"] > cur_ema_fast
			ema_ok_short = last_c["close"] < cur_ema_fast
		else:
			ema_ok_long = True
			ema_ok_short = True

		# ── Detekce signálů ───────────────────────────────────────────────────
		sweep_long = self.detect_liquidity_sweep_long(rates, lookback)
		sweep_short = self.detect_liquidity_sweep_short(rates, lookback)
		bull_eng = self.is_bullish_engulfing(last_c, prev_c)
		bear_eng = self.is_bearish_engulfing(last_c, prev_c)
		strong_lower = self.has_strong_lower_wick(last_c)
		strong_upper = self.has_strong_upper_wick(last_c)

		# ── Filtry ────────────────────────────────────────────────────────────
		ts_utc = datetime.fromtimestamp(last_c["time"], tz=timezone.utc)
		spread_ok = self.passes_spread_filter(symbol, spread)
		atr_ok = self.passes_atr_filter(symbol, rates)
		session_ok = self.passes_session_filter(ts_utc)
		position_ok = self.passes_position_filter(symbol, open_positions)

		# ── LONG scoring ──────────────────────────────────────────────────────
		long_ctx = SignalContext(
			liquidity_sweep_detected=sweep_long,
			engulfing_detected=bull_eng,
			strong_wick_detected=strong_lower,
			ema_context_ok=ema_ok_long,
			atr_ok=atr_ok,
			spread_ok=spread_ok,
			session_ok=session_ok,
			position_ok=position_ok,
		)
		long_score = self.calculate_signal_score(long_ctx)

		# ── SHORT scoring ─────────────────────────────────────────────────────
		short_ctx = SignalContext(
			liquidity_sweep_detected=sweep_short,
			engulfing_detected=bear_eng,
			strong_wick_detected=strong_upper,
			ema_context_ok=ema_ok_short,
			atr_ok=atr_ok,
			spread_ok=spread_ok,
			session_ok=session_ok,
			position_ok=position_ok,
		)
		short_score = self.calculate_signal_score(short_ctx)

		min_score = cfg.get("min_signal_score", 70.0)
		allow_buy = cfg.get("allow_buy", True)
		allow_sell = cfg.get("allow_sell", True)

		# Vybereme silnější signál (pokud oba dosáhly minima, upřednostníme silnější)
		best_side = "HOLD"
		best_score = 0.0
		best_ctx = long_ctx

		if allow_buy and long_score >= min_score and long_score >= short_score:
			best_side = "BUY"
			best_score = long_score
			best_ctx = long_ctx
		elif allow_sell and short_score >= min_score:
			best_side = "SELL"
			best_score = short_score
			best_ctx = short_ctx

		if best_side == "HOLD":
			reasons = []
			if not sweep_long and not sweep_short:
				reasons.append("no_liquidity_sweep")
			if long_score < min_score and short_score < min_score:
				reasons.append(f"score_below_min")
			if not spread_ok:
				reasons.append("spread_too_high")
			if not atr_ok:
				reasons.append("atr_out_of_range")
			if not session_ok:
				reasons.append("outside_session")
			if not position_ok:
				reasons.append("position_limit_reached")
			return self._hold(symbol, ",".join(reasons) or "no_signal", long_score, short_score)

		# ── Výpočty pro management pozice ─────────────────────────────────────
		tp = self.calculate_take_profit(symbol, best_side, last_c, atr, best_score)
		invalidation = self._calc_invalidation(best_side, last_c, local_low, local_high, atr)
		emergency = (
			self.calculate_emergency_reference(symbol, best_side, last_c, atr)
			if cfg.get("use_emergency_stop", True)
			else None
		)

		reason_parts = ["liquidity_sweep"]
		if best_ctx.engulfing_detected:
			reason_parts.append("engulfing")
		if best_ctx.strong_wick_detected:
			reason_parts.append("strong_wick")
		if best_ctx.ema_context_ok:
			reason_parts.append("ema_ok")

		return StrategySignal(
			symbol=symbol,
			side=best_side,
			score=best_score,
			reason="+".join(reason_parts),
			invalidation_level=invalidation,
			take_profit=tp,
			emergency_stop_reference=emergency,
			metadata={
				"atr": atr,
				"local_low": local_low,
				"local_high": local_high,
				"ema_fast": cur_ema_fast,
				"ema_slow": cur_ema_slow,
				"long_score": long_score,
				"short_score": short_score,
				"spread": spread,
				"bar_time": last_c["time"],
				"score_details": best_ctx.score_details,
			},
		)

	# ── Výstupní logika ───────────────────────────────────────────────────────

	def evaluate_exit_conditions(
		self,
		position: Dict[str, Any],
		rates: Optional[np.ndarray],
		spread: float,
		account_state: Dict[str, Any],
	) -> ExitDecision:
		"""
		Vyhodnotí, zda má být existující pozice uzavřena.

		Priority exit podmínek:
		1. Emergency stop reference – okamžité uzavření (urgency=emergency).
		2. Invalidation level – setup byl logicky zneplatněn.
		3. Max floating loss – ochrana kapitálu na úrovni pozice.
		4. Max holding time – pozice nesmí ‚viset' příliš dlouho.
		5. Account drawdown – systémová ochrana celého účtu.
		"""
		cfg = self._cfg or _load_scalping_config()
		ticket = str(position.get("ticket", ""))
		symbol = position.get("symbol", "")
		side = position.get("type", "BUY")
		pnl = float(position.get("pnl", 0.0) or 0.0)
		swap = float(position.get("swap", 0.0) or 0.0)
		net_pnl = pnl + swap
		current_price = float(position.get("current_price", 0.0) or 0.0)

		state = self._position_state.get(ticket, {})
		max_loss = float(state.get("max_floating_loss", cfg.get("max_floating_loss_per_trade", 20.0)))
		max_bars = int(state.get("max_holding_bars", cfg.get("max_holding_bars", 8)))
		tf_minutes = cfg.get("tf_minutes", 5)
		max_hold_minutes = max_bars * tf_minutes
		emergency_ref = state.get("emergency_stop_reference")
		invalidation = state.get("invalidation_level")
		opened_at_str = state.get("opened_at")

		# ── 1. Emergency stop reference ──────────────────────────────────────
		if emergency_ref is not None and current_price > 0:
			hit = (
				(side == "BUY" and current_price <= float(emergency_ref)) or
				(side == "SELL" and current_price >= float(emergency_ref))
			)
			if hit:
				return ExitDecision(
					should_close=True,
					reason="emergency_stop_reference_hit",
					urgency="emergency",
					metadata={"emergency_ref": emergency_ref, "current_price": current_price},
				)

		# ── 2. Invalidation level ────────────────────────────────────────────
		if invalidation is not None and current_price > 0:
			invalidated = (
				(side == "BUY" and current_price < float(invalidation)) or
				(side == "SELL" and current_price > float(invalidation))
			)
			if invalidated:
				return ExitDecision(
					should_close=True,
					reason="invalidation_level_breached",
					urgency="emergency",
					metadata={"invalidation": invalidation, "current_price": current_price},
				)

		# ── 3. Max floating loss ─────────────────────────────────────────────
		if net_pnl < -abs(max_loss):
			return ExitDecision(
				should_close=True,
				reason="max_floating_loss_exceeded",
				urgency="emergency",
				metadata={"net_pnl": net_pnl, "limit": max_loss},
			)

		# ── 4. Max holding time ──────────────────────────────────────────────
		if opened_at_str:
			try:
				opened_at = datetime.fromisoformat(opened_at_str)
				if opened_at.tzinfo is None:
					opened_at = opened_at.replace(tzinfo=timezone.utc)
				hold_minutes = (datetime.now(tz=timezone.utc) - opened_at).total_seconds() / 60.0
				if hold_minutes >= max_hold_minutes:
					return ExitDecision(
						should_close=True,
						reason="max_holding_time_exceeded",
						urgency="normal",
						metadata={"hold_minutes": round(hold_minutes, 1), "max_minutes": max_hold_minutes},
					)
			except (ValueError, TypeError):
				pass

		# ── 5. Account drawdown (nízký nouzový backstop, výchozí 90 %) ────────────────
		# Aktivuje se jen při excesiálních ztrátách. Vstupní podmínka (5 % volná marže)
		# již garantuje, že účet má dostatek kapitálu pro další obchod.
		balance = float(account_state.get("balance", 0.0) or 0.0)
		equity = float(account_state.get("equity", balance) or balance)
		max_dd = cfg.get("max_account_drawdown_percent", 90.0)
		if balance > 0 and ((balance - equity) / balance * 100.0) >= max_dd:
			return ExitDecision(
				should_close=True,
				reason="account_drawdown_limit_reached",
				urgency="emergency",
				metadata={"balance": balance, "equity": equity},
			)

		return ExitDecision(should_close=False, reason="hold")

	# ── Exekuce obchodu ───────────────────────────────────────────────────────

	def open_trade_if_allowed(
		self,
		signal: StrategySignal,
		symbol: str,
		account_state: Dict[str, Any],
		open_positions: List[Dict],
	) -> bool:
		"""
		Provede finální kontrolu všech podmínek a případně otevře obchod.

		Strategie neodesílá duplicitní příkaz – všechny podmínky jsou ověřeny
		atomicky před odesláním na broker.
		"""
		cfg = self._cfg
		strategy_ctx = self._build_strategy_context(cfg)
		strategy_id = cfg["strategy_id"]
		magic = cfg["magic"]

		# ── Limit celkových pozic ────────────────────────────────────────────
		total_open = count_open_positions_for_strategy(open_positions, strategy_ctx)
		max_total = cfg.get("max_positions_total", 3)
		if max_total > 0 and total_open >= max_total:
			self._log.info(f"[SCALP:{symbol}] Limit celkových pozic ({total_open}/{max_total})")
			return False

		# ── Pozice na symbolu ────────────────────────────────────────────────
		sym_positions = [
			p for p in open_positions
			if p.get("symbol") == symbol and position_belongs_to_strategy(p, strategy_ctx)
		]
		if len(sym_positions) >= cfg.get("max_positions_per_symbol", 1):
			self._log.info(f"[SCALP:{symbol}] Pozice na symbolu již existuje")
			return False

		# ── USD expozice (součet abs. P&L strategie) ─────────────────────────
		exposure = sum(
			abs(float(p.get("pnl", 0.0) or 0.0))
			for p in open_positions
			if position_belongs_to_strategy(p, strategy_ctx)
		)
		max_exp = cfg.get("max_usd_exposure", 2.0)
		if exposure >= max_exp:
			self._log.info(f"[SCALP:{symbol}] Max USD expozice ({exposure:.2f}/{max_exp})")
			return False

		# ── Denní ztrátový limit ─────────────────────────────────────────────
		daily_loss = sum(
			abs(float(p.get("pnl", 0.0) or 0.0))
			for p in open_positions
			if position_belongs_to_strategy(p, strategy_ctx) and float(p.get("pnl", 0.0) or 0.0) < 0
		)
		max_daily = cfg.get("max_daily_loss", 100.0)
		if daily_loss >= max_daily:
			self._log.warning(f"[SCALP] Denní ztrátový limit ({daily_loss:.2f}/{max_daily})")
			return False

		# ── Povolený směr obchodu ────────────────────────────────────────────
		if signal.side == "BUY" and not cfg.get("allow_buy", True):
			self._log.info(f"[SCALP:{symbol}] BUY zakázán konfigurací")
			return False
		if signal.side == "SELL" and not cfg.get("allow_sell", True):
			self._log.info(f"[SCALP:{symbol}] SELL zakázán konfigurací")
			return False

		# ── Výpočet lotu ─────────────────────────────────────────────────────
		lot_size = self.calculate_position_size(symbol, signal, account_state)
		if lot_size <= 0.0:
			self._log.warning(f"[SCALP:{symbol}] Neplatný lot: {lot_size}")
			return False

		comment = build_strategy_comment(strategy_id)
		self._log.info(
			f"[SCALP:{symbol}] Otvírám {signal.side} | score={signal.score:.0f} | "
			f"lot={lot_size} | tp={signal.take_profit:.5f} | reason={signal.reason}"
		)

		success = self._ctx.open_trade(
			symbol=symbol,
			action=signal.side,
			lot_size=lot_size,
			take_profit=signal.take_profit,
			strategy_id=strategy_id,
			magic=magic,
			comment=comment,
		)

		if success:
			self._log.info(f"[SCALP:{symbol}] Obchod otevřen")
			self._register_new_position(symbol, signal, lot_size, cfg)
		else:
			self._log.warning(f"[SCALP:{symbol}] Otevření obchodu selhalo")

		return success

	def close_position_if_required(
		self,
		position: Dict[str, Any],
		exit_decision: ExitDecision,
	) -> bool:
		"""Uzavře pozici pokud exit_decision.should_close == True."""
		if not exit_decision.should_close:
			return False

		ticket = int(position.get("ticket", 0))
		symbol = position.get("symbol", "")
		side = position.get("type", "BUY")
		volume = float(position.get("volume", 0.01))
		cfg = self._cfg or _load_scalping_config()
		magic = cfg.get("magic", SCALP_DEFAULT_MAGIC)

		pos_type = mt5.POSITION_TYPE_BUY if side == "BUY" else mt5.POSITION_TYPE_SELL
		comment = f"scalp:{exit_decision.reason}"[:31]  # MT5 limit komentáře

		success = self._ctx.close_position(
			ticket=ticket,
			symbol=symbol,
			position_type=pos_type,
			volume=volume,
			magic=magic,
			comment=comment,
		)

		if success:
			self._log.info(f"[SCALP:{symbol}] Ticket {ticket} uzavřen: {exit_decision.reason}")
			self._position_state.pop(str(ticket), None)
			self._save_position_state()
		else:
			self._log.warning(f"[SCALP:{symbol}] Uzavření ticketu {ticket} selhalo")

		return success

	# ── Position sizing & TP / Emergency ─────────────────────────────────────

	def calculate_position_size(
		self,
		symbol: str,
		signal: StrategySignal,
		account_state: Dict[str, Any],
	) -> float:
		"""
		Vypočítá velikost pozice.

		Dvě metody:
		  fixed:      SCALP_FIXED_VOLUME (výchozí 0.01 lotu)
		  risk-based: SCALP_RISK_PERCENT % z balance děleno ATR stop vzdáleností

		Vrátí 0.0 při chybě výpočtu (zabrání vstupu s neplatným lotem).
		"""
		cfg = self._cfg
		fixed = cfg.get("fixed_volume", 0.01)

		if not cfg.get("use_risk_based_volume", False):
			return fixed

		balance = float(account_state.get("balance", 0.0) or 0.0)
		if balance <= 0:
			return fixed

		risk_usd = balance * (cfg.get("risk_percent", 0.5) / 100.0)
		invalidation = signal.invalidation_level

		tick = mt5.symbol_info_tick(symbol)
		if tick is None:
			return fixed
		entry_price = float(tick.ask if signal.side == "BUY" else tick.bid)

		stop_distance = abs(entry_price - invalidation)
		if stop_distance <= 0:
			return fixed

		sym_info = mt5.symbol_info(symbol)
		if sym_info is None:
			return fixed

		tick_val = float(getattr(sym_info, "trade_tick_value", 0.0) or 0.0)
		tick_sz = float(getattr(sym_info, "trade_tick_size", 0.0) or 0.0)
		if tick_val <= 0 or tick_sz <= 0:
			return fixed

		ticks_in_stop = stop_distance / tick_sz
		risk_per_lot = ticks_in_stop * tick_val
		if risk_per_lot <= 0:
			return fixed

		lot = risk_usd / risk_per_lot
		min_lot = float(getattr(sym_info, "volume_min", 0.01) or 0.01)
		max_lot = float(getattr(sym_info, "volume_max", 100.0) or 100.0)
		lot_step = float(getattr(sym_info, "volume_step", 0.01) or 0.01)

		if lot_step > 0:
			lot = int(lot / lot_step) * lot_step
		lot = max(min_lot, min(lot, max_lot))
		return round(lot, 8)

	def calculate_take_profit(
		self,
		symbol: str,
		side: str,
		last_candle: Dict[str, float],
		atr: float,
		score: float,
	) -> float:
		"""
		Vypočítá cílovou úroveň zisku.

		Score modifikátor (konzervativní u slabšího signálu, agresivní u silného):
		  score blízko min_signal_score  -> multiplier ≈ 0.7 (konzervativní)
		  score blízko 90                -> multiplier ≈ 1.0
		  score >= 100                   -> multiplier ≈ 1.3 (agresivní)

		Režimy:
		  'atr':   TP = entry ± (ATR × tp_atr_multiplier × score_mod)
		  'fixed': TP = entry ± (fixed_tp_points × point × score_mod)
		"""
		cfg = self._cfg
		entry = last_candle["close"]

		min_score = cfg.get("min_signal_score", 70.0)
		score_range = max(105.0 - min_score, 1.0)
		normalized = max(0.0, min(1.0, (score - min_score) / score_range))
		mod = 0.7 + normalized * 0.6  # rozsah 0.7 – 1.3

		if cfg.get("tp_mode", "atr") == "atr":
			distance = atr * cfg.get("tp_atr_multiplier", 0.5) * mod
		else:
			base_sym = _strip_suffix(symbol)
			pts = cfg.get("fixed_tp_by_symbol", {}).get(base_sym, 50.0)
			point = _get_point_size(symbol)
			distance = pts * point * mod

		if side == "BUY":
			return round(entry + distance, 5)
		return round(entry - distance, 5)

	def calculate_emergency_reference(
		self,
		symbol: str,
		side: str,
		last_candle: Dict[str, float],
		atr: float,
	) -> float:
		"""
		Vypočítá krajní záchrannou úroveň (emergency stop reference).

		NENÍ klasický SL – neodesílá se na broker. Strategie ji porovnává
		s aktuální cenou v evaluate_exit_conditions() a při dosažení uzavírá
		pozici okamžitě (urgency='emergency').

		Výchozí nastavení: 1.5× ATR od low/high poslední uzavřené svíčky.
		"""
		mult = get_float_env("SCALP_EMERGENCY_ATR_MULTIPLIER", 1.5)
		if side == "BUY":
			return round(last_candle["low"] - atr * mult, 5)
		return round(last_candle["high"] + atr * mult, 5)

	# ── Pattern detection ─────────────────────────────────────────────────────

	def detect_liquidity_sweep_long(self, rates: np.ndarray, lookback: int) -> bool:
		"""
		Detekuje LONG liquidity sweep (false breakdown pod lokálním minimem).

		Setup (v pořadí):
		  1. Existuje lokální minimum za posledních N svíček (bez poslední).
		  2. Poslední svíčka prorazila toto minimum cenou LOW.
		  3. Poslední svíčka zavřela ZPĚT NAD tímto minimem (odmítnutí).

		Interpretace: trh „smíchal" stop-lossy pod klíčovou úrovní a obrátil se.
		Toto je typický vstup pro long pozici ve směru obratu.
		"""
		n = len(rates)
		if n < lookback + 1:
			return False
		last_idx = n - 1
		window = rates[max(0, last_idx - lookback):last_idx]
		if len(window) == 0:
			return False
		local_low = float(np.min(window["low"]))
		last_low = float(rates[last_idx]["low"])
		last_close = float(rates[last_idx]["close"])
		return last_low < local_low and last_close > local_low

	def detect_liquidity_sweep_short(self, rates: np.ndarray, lookback: int) -> bool:
		"""
		Detekuje SHORT liquidity sweep (false breakout nad lokálním maximem).

		Setup:
		  1. Existuje lokální maximum za posledních N svíček (bez poslední).
		  2. Poslední svíčka prorazila toto maximum cenou HIGH.
		  3. Poslední svíčka zavřela ZPĚT POD tímto maximem (odmítnutí).
		"""
		n = len(rates)
		if n < lookback + 1:
			return False
		last_idx = n - 1
		window = rates[max(0, last_idx - lookback):last_idx]
		if len(window) == 0:
			return False
		local_high = float(np.max(window["high"]))
		last_high = float(rates[last_idx]["high"])
		last_close = float(rates[last_idx]["close"])
		return last_high > local_high and last_close < local_high

	def is_bullish_engulfing(
		self, current: Dict[str, float], previous: Dict[str, float]
	) -> bool:
		"""
		Detekuje bullish engulfing pattern.

		Aktuální bullish svíčka kompletně pohltí tělo předchozí bearish svíčky.
		Silné potvrzení odmítnutí nižší ceny při long liquidity sweep setupu.
		"""
		pc = previous.get("close", 0.0)
		po = previous.get("open", 0.0)
		cc = current.get("close", 0.0)
		co = current.get("open", 0.0)
		return pc < po and cc > co and co <= pc and cc >= po

	def is_bearish_engulfing(
		self, current: Dict[str, float], previous: Dict[str, float]
	) -> bool:
		"""
		Detekuje bearish engulfing pattern.

		Aktuální bearish svíčka kompletně pohltí tělo předchozí bullish svíčky.
		"""
		pc = previous.get("close", 0.0)
		po = previous.get("open", 0.0)
		cc = current.get("close", 0.0)
		co = current.get("open", 0.0)
		return pc > po and cc < co and co >= pc and cc <= po

	def has_strong_lower_wick(self, candle: Dict[str, float]) -> bool:
		"""
		Zkontroluje přítomnost silného spodního knotu (shadow).

		Spodní knot >= SCALP_MIN_LOWER_WICK_RATIO × tělo svíčky (výchozí 2.0×).
		Indikuje, že trh odmítl nízké ceny a vrátil se výše – potvrzení long setupu.
		"""
		o = candle.get("open", 0.0)
		h = candle.get("high", 0.0)
		lo = candle.get("low", 0.0)
		c = candle.get("close", 0.0)
		price_range = h - lo
		if price_range <= 0:
			return False
		body = abs(c - o)
		lower_wick = min(o, c) - lo
		min_ratio = get_float_env("SCALP_MIN_LOWER_WICK_RATIO", 2.0)
		effective_body = max(body, price_range * 0.03)
		return lower_wick >= effective_body * min_ratio

	def has_strong_upper_wick(self, candle: Dict[str, float]) -> bool:
		"""
		Zkontroluje přítomnost silného horního knotu.

		Horní knot >= SCALP_MIN_UPPER_WICK_RATIO × tělo svíčky (výchozí 2.0×).
		Indikuje odmítnutí vysoké ceny – potvrzení short setupu.
		"""
		o = candle.get("open", 0.0)
		h = candle.get("high", 0.0)
		lo = candle.get("low", 0.0)
		c = candle.get("close", 0.0)
		price_range = h - lo
		if price_range <= 0:
			return False
		body = abs(c - o)
		upper_wick = h - max(o, c)
		min_ratio = get_float_env("SCALP_MIN_UPPER_WICK_RATIO", 2.0)
		effective_body = max(body, price_range * 0.03)
		return upper_wick >= effective_body * min_ratio

	# ── Technické indikátory ──────────────────────────────────────────────────

	def calculate_ema(self, series: np.ndarray, period: int) -> Optional[np.ndarray]:
		"""
		Vypočítá exponenciální klouzavý průměr (EMA) bez externích závislostí.

		Seed = SMA prvních `period` hodnot, poté EMA rekurze.
		Výsledné pole má na pozicích < period hodnotu NaN.
		"""
		n = len(series)
		if n < period:
			return None
		k = 2.0 / (period + 1)
		result = np.full(n, np.nan)
		result[period - 1] = float(np.mean(series[:period]))
		for i in range(period, n):
			result[i] = float(series[i]) * k + result[i - 1] * (1.0 - k)
		return result

	def calculate_atr(self, rates: np.ndarray, period: int) -> Optional[float]:
		"""
		Vypočítá ATR (Average True Range) pro poslední uzavřenou svíčku.

		True Range = max(H-L, |H-prev_C|, |L-prev_C|)
		ATR = Wilderovo vyhlazení (alpha = 1/period).
		"""
		n = len(rates)
		if n < period + 1:
			return None

		highs = rates["high"].astype(float)
		lows = rates["low"].astype(float)
		closes = rates["close"].astype(float)

		tr_values = [
			max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
			for i in range(1, n)
		]

		if len(tr_values) < period:
			return None

		atr_val = float(np.mean(tr_values[:period]))
		alpha = 1.0 / period
		for tr in tr_values[period:]:
			atr_val = tr * alpha + atr_val * (1.0 - alpha)
		return atr_val

	# ── Filtry ────────────────────────────────────────────────────────────────

	def passes_spread_filter(self, symbol: str, spread: float) -> bool:
		"""
		Zkontroluje zda je spread pod maximálním povoleným limitem.

		U skalpování je spread kritický: při vysokém spreadu je celý
		potenciální zisk eliminován okamžitě po vstupu do obchodu.
		"""
		cfg = self._cfg
		base = _strip_suffix(symbol)
		max_spread = cfg.get("max_spread_by_symbol", {}).get(
			base, cfg.get("default_max_spread", 30.0)
		)
		return spread <= max_spread

	def passes_atr_filter(self, symbol: str, rates: np.ndarray) -> bool:
		"""
		Zkontroluje zda je ATR v povoleném rozsahu volatility.

		Příliš nízké ATR = trh stagnuje, malé příležitosti pro scalp.
		Příliš vysoké ATR = trh je přehnaně volatilní, větší riziko slippage.
		"""
		cfg = self._cfg
		atr = self.calculate_atr(rates, cfg.get("atr_period", 14))
		if atr is None:
			return False
		base = _strip_suffix(symbol)
		min_a = cfg.get("min_atr_by_symbol", {}).get(base, 0.0001)
		max_a = cfg.get("max_atr_by_symbol", {}).get(base, 99.0)
		return min_a <= atr <= max_a

	def passes_session_filter(self, timestamp_utc: datetime) -> bool:
		"""
		Zkontroluje zda je čas v povoleném obchodním okně.

		Mimo likvidní seance (London, New York) se zvyšuje pravděpodobnost
		false breaků a spreads bývají vyšší. Pátek po 16:00 UTC je blokován.
		"""
		cfg = self._cfg
		if not cfg.get("use_session_filter", True):
			return True

		cutoff = cfg.get("friday_cutoff_hour_utc", 16)
		if timestamp_utc.weekday() == 4 and timestamp_utc.hour >= cutoff:
			return False

		sessions: List[_TradingSession] = cfg.get("sessions", [])
		if not sessions:
			return True

		now_mins = timestamp_utc.hour * 60 + timestamp_utc.minute
		for s in sessions:
			start_mins = s.start_hour * 60 + s.start_minute
			end_mins = s.end_hour * 60 + s.end_minute
			if start_mins <= now_mins < end_mins:
				return True
		return False

	def passes_position_filter(self, symbol: str, open_positions: List[Dict]) -> bool:
		"""Zkontroluje zda není překročen limit pozic na daném symbolu."""
		cfg = self._cfg
		strategy_ctx = self._build_strategy_context(cfg)
		max_per_sym = cfg.get("max_positions_per_symbol", 1)
		sym_count = sum(
			1 for p in open_positions
			if p.get("symbol") == symbol and position_belongs_to_strategy(p, strategy_ctx)
		)
		return sym_count < max_per_sym

	# ── Scoring ───────────────────────────────────────────────────────────────

	def calculate_signal_score(self, context: SignalContext) -> float:
		"""
		Vypočítá celkové skóre kvality signálu (0 – 105 bodů).

		Váhy:
		  +40  Liquidity sweep detekován        (klíčová podmínka)
		  +20  Engulfing potvrzení              (cenové odmítnutí)
		  +15  Silný wick                       (potvrzení odmítnutí)
		  +15  EMA kontext                      (trend alignment)
		  +5   ATR filter                       (volatilita OK)
		  +5   Spread filter                    (spread OK)
		  +5   Session filter                   (seance aktivní)

		Vstup je povolen pouze pokud score >= SCALP_MIN_SIGNAL_SCORE (výchozí 70).
		Minimální profitabilní kombinace: sweep(40) + engulfing(20) + spread(5) + session(5) = 70.
		"""
		score = 0.0
		details: Dict[str, float] = {}

		if context.liquidity_sweep_detected:
			score += _SCORE_LIQUIDITY_SWEEP
			details["liquidity_sweep"] = _SCORE_LIQUIDITY_SWEEP
		if context.engulfing_detected:
			score += _SCORE_ENGULFING
			details["engulfing"] = _SCORE_ENGULFING
		if context.strong_wick_detected:
			score += _SCORE_STRONG_WICK
			details["strong_wick"] = _SCORE_STRONG_WICK
		if context.ema_context_ok:
			score += _SCORE_EMA_CONTEXT
			details["ema_context"] = _SCORE_EMA_CONTEXT
		if context.atr_ok:
			score += _SCORE_ATR_FILTER
			details["atr_filter"] = _SCORE_ATR_FILTER
		if context.spread_ok:
			score += _SCORE_SPREAD_FILTER
			details["spread_filter"] = _SCORE_SPREAD_FILTER
		if context.session_ok:
			score += _SCORE_SESSION_FILTER
			details["session_filter"] = _SCORE_SESSION_FILTER

		context.score_details.update(details)
		return score

	# ── Privátní helpers ──────────────────────────────────────────────────────

	def _build_strategy_context(self, cfg: Dict[str, Any]) -> StrategyContext:
		"""Sestaví StrategyContext z aktuální konfigurace."""
		return StrategyContext(
			strategy_id=cfg.get("strategy_id", SCALP_DEFAULT_STRATEGY_ID),
			magic=cfg.get("magic", SCALP_DEFAULT_MAGIC),
			manage_legacy_positions=False,
			activation_margin_percent=cfg.get("activation_margin_percent", 5.0),
			max_open_positions=cfg.get("max_positions_total", 3),
			session_start_hour_utc=0,
			session_end_hour_utc=24,
			friday_cutoff_hour_utc=cfg.get("friday_cutoff_hour_utc", 16),
		)

	def _activation_gate_passed(
		self, account_state: Dict[str, Any], open_positions: List[Dict]
	) -> bool:
		"""Interní kontrola: splnění podmínek pro otevření nového obchodu."""
		cfg = self._cfg
		balance = float(account_state.get("balance", 0.0) or 0.0)
		raw_free = float(
			account_state.get("raw_margin_free", account_state.get("margin_free", 0.0)) or 0.0
		)
		if balance <= 0:
			return False
		margin_pct = (raw_free / balance) * 100.0
		min_margin = cfg.get("activation_margin_percent", 5.0)
		if margin_pct < min_margin:
			self._log.debug(
				f"[SCALP] Aktivační podmínka nesplněna: marže {margin_pct:.1f}% < {min_margin:.1f}%"
			)
			return False

		strategy_ctx = self._build_strategy_context(cfg)
		max_total = cfg.get("max_positions_total", 3)
		if max_total > 0 and count_open_positions_for_strategy(open_positions, strategy_ctx) >= max_total:
			return False

		return True

	def _calc_invalidation(
		self,
		side: str,
		last_c: Dict[str, float],
		local_low: float,
		local_high: float,
		atr: float,
	) -> float:
		"""Vypočítá invalidation level – logické zneplatnění setupu."""
		if side == "BUY":
			return round(min(local_low, last_c["low"]) - atr * 0.2, 5)
		return round(max(local_high, last_c["high"]) + atr * 0.2, 5)

	def _hold(
		self, symbol: str, reason: str, long_score: float = 0.0, short_score: float = 0.0
	) -> StrategySignal:
		"""Vrátí HOLD signal."""
		return StrategySignal(
			symbol=symbol,
			side="HOLD",
			score=0.0,
			reason=reason,
			invalidation_level=0.0,
			take_profit=0.0,
			metadata={"long_score": long_score, "short_score": short_score},
		)

	# ── State persistence ─────────────────────────────────────────────────────

	def _load_position_state(self) -> None:
		"""Načte metadata pozic z JSON souboru (persistence přes restarty)."""
		if self._state_file is None or not self._state_file.exists():
			return
		try:
			with open(self._state_file, "r", encoding="utf-8") as f:
				data = json.load(f)
			if isinstance(data, dict):
				self._position_state = data
		except Exception as exc:
			self._log.warning(f"[SCALP] Nelze načíst position state: {exc}")

	def _save_position_state(self) -> None:
		"""Uloží metadata pozic do JSON souboru."""
		if self._state_file is None:
			return
		try:
			self._state_file.parent.mkdir(parents=True, exist_ok=True)
			with open(self._state_file, "w", encoding="utf-8") as f:
				json.dump(self._position_state, f, indent=2, default=str)
		except Exception as exc:
			self._log.warning(f"[SCALP] Nelze uložit position state: {exc}")

	def _register_new_position(
		self,
		symbol: str,
		signal: StrategySignal,
		lot_size: float,
		cfg: Dict[str, Any],
	) -> None:
		"""
		Registruje metadata pro nově otevřenou pozici.

		Ticket zjistíme z aktuálně otevřených pozic (nejnovější na symbolu
		s magic číslem strategie). Metadata jsou klíčová pro exit management.
		"""
		try:
			positions = self._ctx.get_open_positions()
		except Exception:
			return

		strategy_ctx = self._build_strategy_context(cfg)
		sym_pos = [
			p for p in positions
			if p.get("symbol") == symbol and position_belongs_to_strategy(p, strategy_ctx)
		]
		if not sym_pos:
			return

		latest = max(sym_pos, key=lambda p: int(p.get("ticket", 0)))
		ticket = str(latest.get("ticket", "unknown"))

		self._position_state[ticket] = {
			"symbol": symbol,
			"side": signal.side,
			"score": signal.score,
			"invalidation_level": signal.invalidation_level,
			"take_profit": signal.take_profit,
			"emergency_stop_reference": signal.emergency_stop_reference,
			"max_floating_loss": cfg.get("max_floating_loss_per_trade", 20.0),
			"max_holding_bars": cfg.get("max_holding_bars", 8),
			"opened_at": datetime.now(tz=timezone.utc).isoformat(),
			"lot_size": lot_size,
			"entry_reason": signal.reason,
			"metadata": signal.metadata,
		}
		self._save_position_state()

	def _cleanup_stale_state(self, open_positions: List[Dict]) -> None:
		"""Odstraní metadata pro pozice, které již nejsou otevřeny."""
		open_tickets = {str(p.get("ticket", "")) for p in open_positions}
		stale = [t for t in list(self._position_state) if t not in open_tickets]
		if stale:
			for t in stale:
				del self._position_state[t]
			self._save_position_state()


# ─────────────────────────────────────────────────────────────────────────────
# Veřejné API – používáno z final_decision.py
# ─────────────────────────────────────────────────────────────────────────────

def is_scalping_strategy_enabled() -> bool:
	"""Vrátí True pokud je SCALP_ENABLED=true (nebo proměnná není nastavena)."""
	return get_bool_env("SCALP_ENABLED", True)


def can_activate_scalping_strategy(
	account_state: Dict[str, Any], open_positions: List[Dict]
) -> bool:
	"""
	Vrátí True pokud jsou splněny podmínky pro aktivaci skalpovací strategie.

	Podmínky:
	  - SCALP_ENABLED=true
	  - volná marže / balance >= SCALP_ACTIVATION_MARGIN_PERCENT (výchozí 5 %)
	  - počet otevřených pozic strategie < SCALP_MAX_POSITIONS_TOTAL

	Tato funkce je volána z final_decision.py jako poslední gate ve fallback řetězci.
	"""
	if not is_scalping_strategy_enabled():
		return False

	cfg = _load_scalping_config()
	balance = float(account_state.get("balance", 0.0) or 0.0)
	raw_free = float(
		account_state.get("raw_margin_free", account_state.get("margin_free", 0.0)) or 0.0
	)
	if balance <= 0:
		return False

	margin_pct = (raw_free / balance) * 100.0
	min_margin = cfg.get("activation_margin_percent", 5.0)
	if margin_pct < min_margin:
		return False

	strategy_ctx = StrategyContext(
		strategy_id=cfg["strategy_id"],
		magic=cfg["magic"],
		manage_legacy_positions=False,
		activation_margin_percent=min_margin,
		max_open_positions=cfg.get("max_positions_total", 3),
		session_start_hour_utc=0,
		session_end_hour_utc=24,
		friday_cutoff_hour_utc=cfg.get("friday_cutoff_hour_utc", 16),
	)
	max_total = cfg.get("max_positions_total", 3)
	if max_total > 0 and count_open_positions_for_strategy(open_positions, strategy_ctx) >= max_total:
		return False

	return True
