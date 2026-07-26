#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Biwenger Money Agent
====================

Agente que se conecta a tu cuenta de **Biwenger** y monitoriza, casi en tiempo
real, el **dinero estimado de tus rivales** en la liga, su **puja máxima** y el
**valor de su equipo**.

IDEA CLAVE
----------
La API de Biwenger NO devuelve el saldo de los demás managers: solo el tuyo.
Igual que hacen las calculadoras tipo Biwenazo, este agente RECONSTRUYE el saldo
de cada rival a partir de:

    dinero = presupuesto_inicial - compras + ventas + bonus_por_jornada (+ ajuste)

Lee el feed público de la liga (`/league/<id>/board`), donde aparecen todos los
fichajes/ventas (`transfer`, `market`, `adminTransfer`) y los cierres de jornada
(`roundFinished`, que reparten dinero por puntos), y los va acumulando por manager
en una base de datos SQLite local.

PUJA MÁXIMA (fórmula de Biwenger):

    puja_maxima = saldo + valor_de_equipo / 4

"Tiempo real" = sondeo (polling) cada N segundos. Biwenger no ofrece push, así que
esto es lo máximo razonable. Usa un intervalo sensato (por defecto 5 min).

ARQUITECTURA
------------
    BiwengerClient -> login + llamadas a la API (endpoints aislados arriba)
    MoneyTracker   -> reconstrucción del saldo por manager
    Storage        -> persistencia SQLite (managers, transacciones, snapshots)
    Monitor        -> bucle de sondeo + detección de cambios + notificaciones
    CLI            -> comandos: once / run / report / reset

AVISO
-----
- Automatizar el acceso puede chocar con los Términos de Servicio de Biwenger.
  Úsalo con tu propia cuenta, con moderación y bajo tu responsabilidad.
- Los ENDPOINTS y NOMBRES DE CAMPO pueden cambiar. Están centralizados en la
  clase BiwengerClient (sección "AJUSTA AQUÍ") y en los métodos _parse_*.
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import requests
except ImportError:  # pragma: no cover
    print("Falta 'requests'. Instala con:  pip install -r requirements.txt")
    sys.exit(1)


log = logging.getLogger("biwenger")


# ---------------------------------------------------------------------------
# Modelos de datos
# ---------------------------------------------------------------------------
@dataclass
class Transaction:
    """Un movimiento del feed: compra, venta o bonus de jornada."""
    tx_id: str          # id único (para deduplicar)
    date: str           # ISO 8601
    manager_id: str     # manager afectado
    kind: str           # "buy" | "sell" | "bonus"
    player: str         # nombre del jugador (o "jornada N" para bonus)
    amount: int         # importe en €


@dataclass
class Manager:
    """Estado financiero reconstruido de un manager de la liga."""
    manager_id: str
    name: str
    initial_budget: int
    team_value: int = 0
    points: int = 0
    purchases: int = 0
    sales: int = 0
    round_bonus: int = 0
    clause_increment: int = 0
    tx_count: int = 0
    bonus: int = 0  # ajuste manual (correcciones, sanciones, etc.)
    balance: int | None = None  # saldo EXACTO si Biwenger lo expone (solo el tuyo)

    @property
    def estimated(self) -> bool:
        """True si el saldo es una estimación (rivales); False si es exacto (tú)."""
        return self.balance is None

    @property
    def cash(self) -> int:
        """Dinero disponible.

        Modo de liga "Plantilla aleatoria + 40M − Valor de Equipo": el saldo de
        cada manager es 40M − valor_de_equipo, más los premios de jornada y menos
        los incrementos de cláusula (dinero que no se ve en el valor de equipo).
        TU saldo se lee exacto de Biwenger; el de los rivales se estima.
        """
        if self.balance is not None:
            return self.balance
        return (self.initial_budget - self.team_value
                + self.round_bonus - self.clause_increment + self.bonus)

    def max_bid(self, overdraft: int = 0) -> int:
        """Puja máxima Biwenger = saldo + valor_equipo/4 (+ margen opcional)."""
        return self.cash + self.team_value // 4 + overdraft


# ---------------------------------------------------------------------------
# Cliente de la API de Biwenger
# ---------------------------------------------------------------------------
class BiwengerClient:
    """
    Cliente HTTP de la API de Biwenger.

    ====================== AJUSTA AQUÍ ======================
    Si Biwenger cambia su API, edita SOLO estas constantes y los métodos
    _parse_* de más abajo. El resto del agente no depende de la forma exacta
    de las respuestas.

    Para obtener league_id, user_id y x_version: abre Biwenger en el navegador,
    abre DevTools -> pestaña Network, pulsa 'Inicio', y mira cualquier petición
    a biwenger.as.com: en sus cabeceras verás X-League, X-User y X-Version.
    """

    BASE = "https://biwenger.as.com/api/v2"
    LOGIN_URL = BASE + "/auth/login"                 # POST {email, password} -> {token}
    ACCOUNT_URL = BASE + "/account"                  # GET  -> tus ligas y tu user id
    # Liga: la identifica la cabecera X-League. 'fields=*,standings' fuerza que
    # cada manager traiga su teamValue (imprescindible para el saldo estimado).
    LEAGUE_URL = BASE + "/league"                    # GET -> standings con teamValue
    USER_URL = BASE + "/user/{uid}"                  # GET -> plantilla (players) y balance
    BOARD_URL = BASE + "/league/{lid}/board"         # GET -> feed de movimientos
    # Base de datos pública de jugadores de LaLiga (sin login):
    COMPETITION_URL = "https://cf.biwenger.com/api/v2/competitions/la-liga/data"
    # tipos de movimiento que afectan al dinero:
    BOARD_TYPES = "transfer,market,adminTransfer,roundFinished,clauseIncrement"
    POSITIONS = {1: "POR", 2: "DEF", 3: "MED", 4: "DEL", 5: "ENT"}
    # =========================================================

    def __init__(self, email: str, password: str,
                 league_id: str | None = None, user_id: str | None = None,
                 x_version: str | None = None, timeout: int = 20):
        self.email = email
        self.password = password
        self.league_id = league_id
        self.my_id = user_id
        self.x_version = x_version
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Lang": "es",
            "User-Agent": "biwenger-agent/1.0 (+personal use)",
        })
        self.token: str | None = None
        self.league_name: str | None = None
        self.league_mode: str | None = None
        self._last_league: dict[str, Any] = {}   # último payload de liga (para ownership)

    # -- autenticación ------------------------------------------------------
    def login(self) -> None:
        log.debug("Autenticando en Biwenger como %s", self.email)
        resp = self.session.post(
            self.LOGIN_URL,
            json={"email": self.email, "password": self.password},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data.get("token") or data.get("data", {}).get("token")
        if not self.token:
            raise RuntimeError(
                "No se encontró token en la respuesta de login. Revisa LOGIN_URL.\n"
                + json.dumps(data)[:400]
            )
        self.session.headers["Authorization"] = f"Bearer {self.token}"

        # Si no nos han dado league_id/user_id, los deducimos de /account.
        if not self.league_id or not self.my_id:
            self._autodetect_league()

        self.session.headers["X-League"] = str(self.league_id)
        self.session.headers["X-User"] = str(self.my_id)
        if self.x_version:
            self.session.headers["X-Version"] = str(self.x_version)
        log.info("Login OK. league=%s user=%s", self.league_id, self.my_id)

    def _autodetect_league(self) -> None:
        resp = self.session.get(self.ACCOUNT_URL, timeout=self.timeout)
        resp.raise_for_status()
        acc = resp.json().get("data", {})
        leagues = acc.get("leagues") or []
        if not leagues:
            raise RuntimeError("No se encontraron ligas en tu cuenta.")
        # Elegimos la liga indicada, o la primera.
        chosen = None
        if self.league_id:
            chosen = next((l for l in leagues if str(l.get("id")) == str(self.league_id)), None)
        chosen = chosen or leagues[0]
        self.league_id = str(chosen.get("id"))
        user = chosen.get("user") or {}
        self.my_id = str(user.get("id") or acc.get("id") or "")
        if len(leagues) > 1:
            log.info("Tienes %d ligas; usando '%s' (id=%s). Fíjala en config.ini "
                     "con league_id si no es la correcta.",
                     len(leagues), chosen.get("name"), self.league_id)

    # -- datos --------------------------------------------------------------
    def fetch_managers(self) -> list[dict[str, Any]]:
        resp = self.session.get(
            self.LEAGUE_URL,
            params={"include": "all", "fields": "*,standings"},
            timeout=self.timeout)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        self._last_league = data if isinstance(data, dict) else {}
        self.league_name = self._last_league.get("name") or self.league_name
        self.league_mode = self._last_league.get("mode") or self.league_mode
        return self._parse_managers(payload)

    def fetch_my_balance(self) -> int | None:
        """Tu saldo EXACTO. Biwenger solo expone 'balance' para tu propio usuario."""
        if not self.my_id:
            return None
        try:
            resp = self.session.get(self.USER_URL.format(uid=self.my_id),
                                    params={"fields": "*"}, timeout=self.timeout)
            resp.raise_for_status()
            d = resp.json().get("data", {})
            b = d.get("balance")
            return int(b) if b is not None else None
        except Exception as e:  # pragma: no cover
            log.warning("No se pudo leer tu balance exacto: %s", e)
            return None

    def fetch_squads(self, manager_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """Plantilla de cada manager: [{id, owner:{clause,...}}]. Un GET por manager."""
        squads: dict[str, list[dict[str, Any]]] = {}
        for uid in manager_ids:
            try:
                resp = self.session.get(self.USER_URL.format(uid=uid),
                                        params={"fields": "players(id,owner)"},
                                        timeout=self.timeout)
                resp.raise_for_status()
                d = resp.json().get("data", {})
                pls = d.get("players") or []
                if pls:
                    squads[str(uid)] = pls
            except Exception as e:  # pragma: no cover
                log.warning("No se pudo leer la plantilla de %s: %s", uid, e)
        return squads

    def fetch_ownership(self) -> dict[str, dict[str, Any]]:
        """A partir del último payload de liga, mapea playerId -> dueño (best-effort)."""
        owners: dict[str, dict[str, Any]] = {}
        for s in (self._last_league.get("standings") or []):
            oid, oname = str(s.get("id")), s.get("name")
            for p in (s.get("players") or []):
                pid = p.get("id") if isinstance(p, dict) else p
                if pid is not None:
                    owners[str(pid)] = {"ownerId": oid, "ownerName": oname}
        return owners

    def fetch_all_players(self) -> list[dict[str, Any]]:
        """Base de datos pública de jugadores de LaLiga (no requiere login).

        Endpoint público en otro host (cf.biwenger.com): lo pedimos SIN las
        cabeceras de sesión/auth de la API privada para que no lo rechace.
        """
        resp = requests.get(self.COMPETITION_URL,
                            params={"lang": "es", "score": 2},
                            headers={"Accept": "application/json"},
                            timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        players = data.get("players", {})
        teams = data.get("teams", {})
        items = players.values() if isinstance(players, dict) else players
        out = []
        for p in items:
            tid = str(p.get("teamID") or p.get("team") or "")
            team = (teams.get(tid) or {}).get("name") if isinstance(teams, dict) else None
            out.append({
                "id": str(p.get("id")),
                "name": p.get("name") or "?",
                "position": self.POSITIONS.get(p.get("position"), "?"),
                "team": team or "?",
                "value": int(p.get("price") or 0),
                "points": int(p.get("points") or 0),
                "status": p.get("status") or "ok",
            })
        return out

    def fetch_transactions(self, pages: int = 1, page_size: int = 50) -> list[Transaction]:
        """Recorre el board (más reciente primero). En la 1ª ejecución sube 'pages'
        para reconstruir desde el inicio de liga; luego basta con 1 página."""
        out: list[Transaction] = []
        for p in range(pages):
            resp = self.session.get(
                self.BOARD_URL.format(lid=self.league_id),
                params={"type": self.BOARD_TYPES,
                        "offset": p * page_size, "limit": page_size},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            batch = self._parse_board(resp.json())
            if not batch:
                break
            out.extend(batch)
        return out

    # -- parsers (adáptalos si cambian los nombres de campo) ---------------
    @staticmethod
    def _parse_managers(payload: Any) -> list[dict[str, Any]]:
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        standings = data.get("standings") or data.get("users") or []
        out = []
        for s in standings:
            out.append({
                "manager_id": str(s.get("id")),
                "name": s.get("name") or "?",
                "team_value": int(s.get("teamValue") or 0),
                "points": int(s.get("points") or 0),
            })
        return out

    @staticmethod
    def _parse_board(payload: Any) -> list[Transaction]:
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        # el board puede venir como lista o dentro de data
        items = data if isinstance(data, list) else (data.get("items") or data.get("board") or [])
        txs: list[Transaction] = []
        for mv in items:
            mtype = mv.get("type")
            date_iso = _to_iso(mv.get("date"))

            if mtype in ("transfer", "market", "adminTransfer"):
                for c in (mv.get("content") or []):
                    amount = int(c.get("amount") or 0)
                    player = _player_name(c.get("player"))
                    buyer = (c.get("to") or {}).get("id") if isinstance(c.get("to"), dict) else None
                    seller = (c.get("from") or {}).get("id") if isinstance(c.get("from"), dict) else None
                    base_id = str(mv.get("id") or mv.get("date"))
                    # Comprador: sale dinero
                    if buyer:
                        txs.append(Transaction(
                            f"{base_id}-{player}-b{buyer}", date_iso,
                            str(buyer), "buy", player, amount))
                    # Vendedor: entra dinero
                    if seller:
                        txs.append(Transaction(
                            f"{base_id}-{player}-s{seller}", date_iso,
                            str(seller), "sell", player, amount))

            elif mtype == "roundFinished":
                content = mv.get("content") or {}
                rnd = (content.get("round") or {}).get("name") or content.get("round") or "?"
                for r in (content.get("results") or []):
                    bonus = int(r.get("bonus") or 0)
                    uid = (r.get("user") or {}).get("id") if isinstance(r.get("user"), dict) else r.get("user")
                    if uid and bonus:
                        txs.append(Transaction(
                            f"round-{rnd}-{uid}", date_iso,
                            str(uid), "bonus", f"jornada {rnd}", bonus))

            elif mtype == "clauseIncrement":
                # Subida de cláusula: el manager PAGA ese dinero (sale de su saldo)
                # y no se refleja en el valor de equipo. Hay que restarlo.
                base_id = str(mv.get("id") or mv.get("date"))
                for c in (mv.get("content") or []):
                    uid = (c.get("user") or {}).get("id") if isinstance(c.get("user"), dict) else c.get("user")
                    amount = int(c.get("amount") or 0)
                    if uid and amount:
                        txs.append(Transaction(
                            f"clause-{base_id}-{uid}", date_iso,
                            str(uid), "clause", "cláusula", amount))
        return txs


def _to_iso(ts: Any) -> str:
    """El board usa timestamps unix; los normalizamos a ISO."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return str(ts or datetime.now(timezone.utc).isoformat())


def _player_name(player: Any) -> str:
    if isinstance(player, dict):
        return player.get("name") or str(player.get("id") or "?")
    return str(player or "?")


# ---------------------------------------------------------------------------
# Reconstrucción del saldo
# ---------------------------------------------------------------------------
class MoneyTracker:
    """Acumula transacciones y reconstruye el saldo de cada manager."""

    def __init__(self, initial_budget: int, overrides: dict[str, int] | None = None,
                 overdraft: int = 0, bonuses: dict[str, int] | None = None):
        self.default_initial = initial_budget
        self.overrides = overrides or {}   # {manager_id: presupuesto_inicial}
        self.overdraft = overdraft
        self.manual_bonuses = bonuses or {}
        self.managers: dict[str, Manager] = {}

    def upsert_manager(self, manager_id: str, name: str,
                       team_value: int = 0, points: int = 0) -> Manager:
        m = self.managers.get(manager_id)
        if m is None:
            m = Manager(
                manager_id=manager_id,
                name=name,
                initial_budget=self.overrides.get(manager_id, self.default_initial),
                bonus=self.manual_bonuses.get(manager_id, 0),
            )
            self.managers[manager_id] = m
        m.name = name or m.name
        m.team_value = team_value or m.team_value
        m.points = points or m.points
        return m

    def apply(self, tx: Transaction) -> None:
        m = self.managers.get(tx.manager_id) or self.upsert_manager(tx.manager_id, "?")
        if tx.kind == "buy":
            m.purchases += tx.amount
        elif tx.kind == "sell":
            m.sales += tx.amount
        elif tx.kind == "bonus":
            m.round_bonus += tx.amount
        elif tx.kind == "clause":
            m.clause_increment += tx.amount
        m.tx_count += 1

    def table(self) -> list[Manager]:
        return sorted(self.managers.values(), key=lambda x: x.cash, reverse=True)


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------
class Storage:
    """SQLite: transacciones (deduplicadas) + estado de managers + histórico."""

    def __init__(self, path: str = "biwenger.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            tx_id TEXT PRIMARY KEY,
            date TEXT, manager_id TEXT, kind TEXT, player TEXT, amount INTEGER
        );
        CREATE TABLE IF NOT EXISTS managers (
            manager_id TEXT PRIMARY KEY,
            name TEXT, initial_budget INTEGER, team_value INTEGER, points INTEGER,
            purchases INTEGER, sales INTEGER, round_bonus INTEGER,
            tx_count INTEGER, bonus INTEGER
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            ts TEXT, manager_id TEXT, cash INTEGER
        );
        """)
        self.conn.commit()

    def known_tx_ids(self) -> set[str]:
        cur = self.conn.execute("SELECT tx_id FROM transactions")
        return {r["tx_id"] for r in cur.fetchall()}

    def save_transactions(self, txs: Iterable[Transaction]) -> int:
        new = 0
        for t in txs:
            try:
                self.conn.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?)",
                                  (t.tx_id, t.date, t.manager_id, t.kind, t.player, t.amount))
                new += 1
            except sqlite3.IntegrityError:
                pass
        self.conn.commit()
        return new

    def load_transactions(self) -> list[Transaction]:
        cur = self.conn.execute("SELECT * FROM transactions ORDER BY date ASC")
        return [Transaction(r["tx_id"], r["date"], r["manager_id"],
                            r["kind"], r["player"], r["amount"]) for r in cur.fetchall()]

    def save_managers(self, managers: Iterable[Manager]) -> None:
        for m in managers:
            self.conn.execute(
                "INSERT OR REPLACE INTO managers VALUES (?,?,?,?,?,?,?,?,?,?)",
                (m.manager_id, m.name, m.initial_budget, m.team_value, m.points,
                 m.purchases, m.sales, m.round_bonus, m.tx_count, m.bonus))
        self.conn.commit()

    def load_bonuses(self) -> dict[str, int]:
        cur = self.conn.execute("SELECT manager_id, bonus FROM managers")
        return {r["manager_id"]: r["bonus"] or 0 for r in cur.fetchall()}

    def snapshot(self, managers: Iterable[Manager]) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        for m in managers:
            self.conn.execute("INSERT INTO snapshots VALUES (?,?,?)",
                              (ts, m.manager_id, m.cash))
        self.conn.commit()

    def last_cash(self) -> dict[str, int]:
        cur = self.conn.execute("""
            SELECT manager_id, cash FROM snapshots
            WHERE ts = (SELECT MAX(ts) FROM snapshots)""")
        return {r["manager_id"]: r["cash"] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Notificaciones (opcional)
# ---------------------------------------------------------------------------
class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, text: str) -> None:
        try:
            requests.post(self.url, json={"chat_id": self.chat_id, "text": text}, timeout=10)
        except Exception as e:  # pragma: no cover
            log.warning("No se pudo enviar Telegram: %s", e)


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------
def fmt(n: int) -> str:
    return f"{n:,.0f} €".replace(",", ".")


class Monitor:
    def __init__(self, client: BiwengerClient, tracker: MoneyTracker,
                 storage: Storage, notifier: TelegramNotifier | None = None,
                 first_pages: int = 40):
        self.client = client
        self.tracker = tracker
        self.storage = storage
        self.notifier = notifier
        self.first_pages = first_pages   # páginas a bajar la 1ª vez (histórico)
        self._bootstrapped = False

    def _rebuild_from_db(self) -> None:
        txs = self.storage.load_transactions()
        for t in txs:
            self.tracker.apply(t)
        if txs:
            self._bootstrapped = True

    def refresh(self) -> list[Transaction]:
        for md in self.client.fetch_managers():
            self.tracker.upsert_manager(md["manager_id"], md["name"],
                                        md["team_value"], md["points"])
        # Tu saldo EXACTO (Biwenger solo lo expone para tu propio usuario).
        my_balance = self.client.fetch_my_balance()
        me = self.tracker.managers.get(str(self.client.my_id))
        if me is not None and my_balance is not None:
            me.balance = my_balance
        # Movimientos del tablón (premios de jornada, cláusulas). En pretemporada
        # puede no existir todavía; que un fallo del board no rompa el resto.
        new_txs: list[Transaction] = []
        try:
            pages = 1 if self._bootstrapped else self.first_pages
            txs = self.client.fetch_transactions(pages=pages)
            known = self.storage.known_tx_ids()
            new_txs = [t for t in txs if t.tx_id not in known]
            for t in new_txs:
                self.tracker.apply(t)
            self.storage.save_transactions(new_txs)
        except Exception as e:  # pragma: no cover
            log.warning("No se pudo leer el tablón (normal en pretemporada): %s", e)
        self.storage.save_managers(self.tracker.managers.values())
        self._bootstrapped = True
        return new_txs

    def report(self) -> str:
        me = self.client.my_id
        lines = [f"{'MANAGER':<22}{'DINERO EST.':>16}{'PUJA MÁX.':>16}{'VALOR EQUIPO':>16}",
                 "-" * 70]
        for m in self.tracker.table():
            mark = " (tú)" if m.manager_id == me else ""
            lines.append(f"{(m.name + mark):<22}{fmt(m.cash):>16}"
                         f"{fmt(m.max_bid(self.tracker.overdraft)):>16}"
                         f"{fmt(m.team_value):>16}")
        return "\n".join(lines)

    def detect_changes(self) -> list[str]:
        prev = self.storage.last_cash()
        msgs = []
        for m in self.tracker.table():
            old = prev.get(m.manager_id)
            if old is not None and old != m.cash:
                delta = m.cash - old
                arrow = "▲" if delta > 0 else "▼"
                msgs.append(f"{arrow} {m.name}: {fmt(old)} → {fmt(m.cash)} "
                            f"({'+' if delta>0 else ''}{fmt(delta)})")
        return msgs

    def run_once(self, quiet: bool = False) -> None:
        new_txs = self.refresh()
        changes = self.detect_changes()
        self.storage.snapshot(self.tracker.managers.values())
        if not quiet:
            print(self.report())
        if new_txs:
            log.info("%d movimiento(s) nuevo(s).", len(new_txs))
        for c in changes:
            log.info(c)
            if self.notifier:
                self.notifier.send(c)

    def export(self, path: str, include_players: bool = True) -> dict:
        """Genera el data.json que consume la app: ajustes de liga, dinero de
        todos los managers y (best-effort) todos los jugadores con su dueño."""
        self._rebuild_from_db()
        self.client.login()
        self.refresh()  # actualiza managers + dinero

        name_by_id = {m.manager_id: m.name for m in self.tracker.table()}
        managers = []
        for m in self.tracker.table():
            managers.append({
                "id": m.manager_id, "name": m.name,
                "cash": m.cash, "maxBid": m.max_bid(self.tracker.overdraft),
                "teamValue": m.team_value, "points": m.points,
                "estimated": m.estimated,
                "purchases": m.purchases, "sales": m.sales,
                "roundBonus": m.round_bonus, "clauseIncrement": m.clause_increment,
                "txCount": m.tx_count,
                "isYou": m.manager_id == str(self.client.my_id),
            })

        # Jugadores CON DUEÑO: la plantilla de cada manager (players id + cláusula)
        # cruzada con la base pública de LaLiga (nombre, posición, equipo, valor).
        players = []
        if include_players:
            try:
                meta = {p["id"]: p for p in self.client.fetch_all_players()}
                squads = self.client.fetch_squads(name_by_id.keys())
                for owner_id, squad in squads.items():
                    for p in squad:
                        pid = str(p.get("id") if isinstance(p, dict) else p)
                        info = meta.get(pid, {})
                        owner = p.get("owner") if isinstance(p, dict) else None
                        clause = (owner or {}).get("clause") if isinstance(owner, dict) else None
                        players.append({
                            "id": pid,
                            "name": info.get("name") or ("#" + pid),
                            "position": info.get("position") or "OTH",
                            "team": info.get("team") or "?",
                            "value": int(info.get("value") or clause or 0),
                            "points": int(info.get("points") or 0),
                            "status": info.get("status") or "ok",
                            "ownerId": owner_id,
                            "ownerName": name_by_id.get(owner_id),
                        })
            except Exception as e:  # los jugadores son opcionales
                log.warning("No se pudieron cargar los jugadores: %s", e)

        data = {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "league": {
                "id": self.client.league_id,
                "name": self.client.league_name or "Mi liga",
                "mode": self.client.league_mode,
                "initialBudget": self.tracker.default_initial,
                "overdraft": self.tracker.overdraft,
                "memberCount": len(managers),
                "youId": self.client.my_id,
            },
            "managers": managers,
            "players": players,
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        log.info("Exportado %s (%d managers, %d jugadores).",
                 path, len(managers), len(players))
        return data

    def run_forever(self, interval: int) -> None:
        log.info("Monitorizando cada %ds. Ctrl+C para parar.", interval)
        self._rebuild_from_db()
        while True:
            try:
                self.client.login()
                self.run_once()
            except KeyboardInterrupt:
                print("\nParado.")
                break
            except Exception as e:
                log.error("Error en el ciclo: %s", e)
            time.sleep(interval)


# ---------------------------------------------------------------------------
# Configuración / CLI
# ---------------------------------------------------------------------------
def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not Path(path).exists():
        print(f"No existe {path}. Copia config.example.ini a {path} y edítalo.")
        sys.exit(1)
    cfg.read(path)
    return cfg


def build_monitor(cfg: configparser.ConfigParser) -> Monitor:
    c = cfg["biwenger"]
    client = BiwengerClient(
        email=c["email"], password=c["password"],
        league_id=c.get("league_id") or None,
        user_id=c.get("user_id") or None,
        x_version=c.get("x_version") or None,
    )
    storage = Storage(c.get("db", "biwenger.db"))

    overrides = {}
    if cfg.has_section("presupuestos"):
        overrides = {k: int(v) for k, v in cfg["presupuestos"].items()}

    tracker = MoneyTracker(
        initial_budget=int(c.get("presupuesto_inicial", "40000000")),
        overrides=overrides,
        overdraft=int(c.get("overdraft", "0")),
        bonuses=storage.load_bonuses(),  # respeta correcciones manuales previas
    )

    notifier = None
    if cfg.has_section("telegram") and cfg["telegram"].get("token"):
        notifier = TelegramNotifier(cfg["telegram"]["token"], cfg["telegram"]["chat_id"])

    return Monitor(client, tracker, storage, notifier,
                   first_pages=int(c.get("paginas_historico", "40")))


def main() -> None:
    p = argparse.ArgumentParser(description="Agente de dinero de rivales en Biwenger")
    p.add_argument("comando", choices=["once", "run", "report", "reset", "export"],
                   help="once=una pasada | run=bucle | report=tabla | "
                        "export=genera data.json para la app | reset=borra la BD")
    p.add_argument("-o", "--out", default="data.json", help="ruta del data.json (export)")
    p.add_argument("-c", "--config", default="config.ini")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    cfg = load_config(args.config)

    if args.comando == "reset":
        db = cfg["biwenger"].get("db", "biwenger.db")
        Path(db).unlink(missing_ok=True)
        print(f"Base de datos {db} borrada.")
        return

    mon = build_monitor(cfg)

    if args.comando == "report":
        mon._rebuild_from_db()
        try:
            mon.client.login()
            mon.refresh()
        except Exception as e:
            log.warning("Sin conexión, muestro estado guardado: %s", e)
        print(mon.report())
    elif args.comando == "once":
        mon._rebuild_from_db()
        mon.client.login()
        mon.run_once()
    elif args.comando == "run":
        mon.run_forever(int(cfg["biwenger"].get("intervalo", "300")))
    elif args.comando == "export":
        mon.export(args.out)


if __name__ == "__main__":
    main()
