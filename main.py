import streamlit as st
import pandas as pd
import datetime
from datetime import timedelta, date
import plotly.express as px
import plotly.graph_objects as go
import time
import math
import sqlite3
import os
import json
from pathlib import Path

plotly_events_import_error = None
try:
    from streamlit_plotly_events import plotly_events
except ImportError as exc:
    plotly_events = None
    plotly_events_import_error = exc


# ============================================================================
# PERSISTENCIA DE DATOS CON SQLITE
# ============================================================================

def get_db_path():
    """Retorna la ruta de la base de datos SQLite en .streamlit/
    Compatible con Streamlit Cloud."""
    db_dir = Path(".streamlit")
    db_dir.mkdir(exist_ok=True)  # Crear directorio si no existe
    db_path = db_dir / "planificacion_data.db"
    return str(db_path)


def init_database():
    """Crea las tablas de la base de datos si no existen.
    Activa configuración segura de SQLite (WAL, foreign_keys)."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Activar modo WAL para mejor concurrencia
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA foreign_keys = ON")
    
    # Tabla de registros (buques/planificaciones) con versionado temporal
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            linea TEXT NOT NULL,
            servicio TEXT NOT NULL,
            buque TEXT NOT NULL,
            rendimiento REAL NOT NULL,
            movimientos INTEGER NOT NULL,
            fecha_inicio TEXT NOT NULL,
            turno_inicio TEXT NOT NULL,
            week_start TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    """)
    
    # Tabla de asignación de grúas con versionado temporal
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS crane_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buque TEXT NOT NULL,
            inicio TEXT NOT NULL,
            sts REAL NOT NULL,
            mhc REAL NOT NULL,
            rend_sts REAL NOT NULL,
            rend_mhc REAL NOT NULL,
            turnos INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    """)
    
    conn.commit()
    conn.close()


def get_current_week_start():
    """Retorna el lunes de la semana actual en formato YYYY-MM-DD."""
    today = datetime.date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


def load_data(week_start=None, user_id="default"):
    """Carga registros y asignaciones de grúas solo de la semana activa y usuario.
    
    Args:
        week_start: Fecha en formato YYYY-MM-DD. Si es None, usa la semana actual.
        user_id: ID del usuario. Por defecto "default" para compatibilidad.
    
    Retorna:
        (records, crane_table): Listas de datos activos para la semana/usuario.
    """
    if week_start is None:
        week_start = get_current_week_start()
    
    db_path = get_db_path()
    
    # Si la BD no existe, retornar datos vacíos
    if not os.path.exists(db_path):
        return [], []
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Cargar registros activos de la semana actual
        cursor.execute("""
            SELECT linea, servicio, buque, rendimiento, movimientos, fecha_inicio, turno_inicio
            FROM records
            WHERE week_start = ? AND user_id = ? AND is_active = 1
            ORDER BY created_at DESC
        """, (week_start, user_id))
        records_rows = cursor.fetchall()
        records = []
        for row in records_rows:
            records.append({
                "linea": row[0],
                "servicio": row[1],
                "buque": row[2],
                "rendimiento": row[3],
                "movimientos": row[4],
                "fecha_inicio": datetime.datetime.strptime(row[5], "%Y-%m-%d").date(),
                "turno_inicio": row[6],
            })
        
        # Cargar asignaciones activas de grúas de la semana actual
        cursor.execute("""
            SELECT buque, inicio, sts, mhc, rend_sts, rend_mhc, turnos
            FROM crane_assignments
            WHERE week_start = ? AND user_id = ? AND is_active = 1
            ORDER BY created_at DESC
        """, (week_start, user_id))
        crane_rows = cursor.fetchall()
        crane_table = []
        for row in crane_rows:
            crane_table.append({
                "Buque": row[0],
                "inicio": row[1],
                "STS": row[2],
                "MHC": row[3],
                "Rend. STS": row[4],
                "Rend. MHC": row[5],
                "Turnos": row[6],
            })
        
        conn.close()
        return records, crane_table
    
    except Exception as e:
        st.error(f"Error al cargar datos: {e}")
        return [], []


def save_records(records, week_start=None, user_id="default"):
    """Guarda registros sin borrar histórico. Inserta nuevas versiones.
    
    Args:
        records: Lista de registros a guardar.
        week_start: Fecha en formato YYYY-MM-DD. Si es None, usa la semana actual.
        user_id: ID del usuario. Por defecto "default".
    """
    if week_start is None:
        week_start = get_current_week_start()
    
    db_path = get_db_path()
    init_database()  # Asegurar que la BD existe
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Desactivar registros viejos de esta semana/usuario en lugar de borrar
        cursor.execute("""
            UPDATE records
            SET is_active = 0, updated_at = ?
            WHERE week_start = ? AND user_id = ? AND is_active = 1
        """, (datetime.datetime.now().isoformat(), week_start, user_id))
        
        # Insertar nuevos registros como activos (versionado)
        now = datetime.datetime.now().isoformat()
        for record in records:
            fecha_str = record["fecha_inicio"].isoformat() if isinstance(record["fecha_inicio"], date) else str(record["fecha_inicio"])
            cursor.execute("""
                INSERT INTO records (linea, servicio, buque, rendimiento, movimientos, fecha_inicio, turno_inicio, week_start, user_id, created_at, updated_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                record.get("linea", ""),
                record.get("servicio", ""),
                record.get("buque", ""),
                float(record.get("rendimiento", 0)),
                int(record.get("movimientos", 0)),
                fecha_str,
                record.get("turno_inicio", "T1"),
                week_start,
                user_id,
                now,
                now,
            ))
        
        conn.commit()
        conn.close()
    
    except Exception as e:
        st.error(f"Error al guardar registros: {e}")


def save_crane_table(crane_table, week_start=None, user_id="default"):
    """Guarda asignaciones de grúas sin borrar histórico. Inserta nuevas versiones.
    
    Args:
        crane_table: Lista de asignaciones a guardar.
        week_start: Fecha en formato YYYY-MM-DD. Si es None, usa la semana actual.
        user_id: ID del usuario. Por defecto "default".
    """
    if week_start is None:
        week_start = get_current_week_start()
    
    db_path = get_db_path()
    init_database()  # Asegurar que la BD existe
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Desactivar asignaciones viejas en lugar de borrar
        cursor.execute("""
            UPDATE crane_assignments
            SET is_active = 0, updated_at = ?
            WHERE week_start = ? AND user_id = ? AND is_active = 1
        """, (datetime.datetime.now().isoformat(), week_start, user_id))
        
        # Insertar nuevas asignaciones como activas (versionado)
        now = datetime.datetime.now().isoformat()
        for crane in crane_table:
            cursor.execute("""
                INSERT INTO crane_assignments (buque, inicio, sts, mhc, rend_sts, rend_mhc, turnos, week_start, user_id, created_at, updated_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                crane.get("Buque", ""),
                crane.get("inicio", ""),
                float(crane.get("STS", 0)),
                float(crane.get("MHC", 0)),
                float(crane.get("Rend. STS", 0)),
                float(crane.get("Rend. MHC", 0)),
                int(crane.get("Turnos", 0)),
                week_start,
                user_id,
                now,
                now,
            ))
        
        conn.commit()
        conn.close()
    
    except Exception as e:
        st.error(f"Error al guardar asignaciones de grúas: {e}")


plotly_events_import_error = None
try:
    from streamlit_plotly_events import plotly_events
except ImportError as exc:
    plotly_events = None
    plotly_events_import_error = exc


def safe_rerun():
    """Try to rerun the Streamlit script. If `st.experimental_rerun` is not available,
    fallback to toggling a session_state key and stop.
    """
    try:
        # Preferred method
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            # Fallback: toggle a session key to force UI update without using
            # deprecated query-params experimental API.
            st.session_state["_refresh"] = not st.session_state.get("_refresh", False)
            # Stop execution now; the UI should refresh on next interaction
            st.stop()

# Mapeo: servicio -> líneas
LINE_SERVICES = {
    "AFS": ["ML"],
    "WS3": ["HL"],
    "NAN": ["ML", "HL"],
    "AN1": ["HL", "ONE", "MSC", "HMM"],
    "LLAMA": ["MSC"],
    "Cabotaje": ["TM"],
}


def ensure_session():
    """Inicializa session_state con datos persistentes de la semana activa.
    Recarga automáticamente cuando cambia la semana."""
    # Inicializar week_offset si no existe
    if "week_offset" not in st.session_state:
        st.session_state.week_offset = 0
    
    # Calcular semana activa basada en week_offset
    today = datetime.date.today()
    current_week_start = today - timedelta(days=today.weekday())
    week_offset = st.session_state.get("week_offset", 0)
    active_week_start = (current_week_start + timedelta(weeks=week_offset)).isoformat()
    
    # Guardar en session_state para usar en otras funciones
    st.session_state._active_week_start = active_week_start
    
    # Cargar datos de la semana activa (siempre, para soportar navegación de semanas)
    records, crane_table = load_data(week_start=active_week_start, user_id="default")
    st.session_state.records = records
    st.session_state.crane_table = crane_table


def get_active_week_start():
    """Obtiene el lunes de la semana actualmente mostrada en el UI.
    Usa week_offset de session_state para calcular la fecha."""
    current_week_start = datetime.date.today() - timedelta(days=datetime.date.today().weekday())
    week_offset = st.session_state.get("week_offset", 0)
    active_week_start = current_week_start + timedelta(weeks=week_offset)
    return active_week_start.isoformat()


def save_records_session(records):
    """Wrapper para save_records() que automáticamente obtiene semana y usuario actuales."""
    # Usar la semana activa ya calculada en ensure_session()
    week_start = st.session_state.get("_active_week_start", get_current_week_start())
    save_records(records, week_start=week_start, user_id="default")


def save_crane_table_session(crane_table):
    """Wrapper para save_crane_table() que automáticamente obtiene semana y usuario actuales."""
    # Usar la semana activa ya calculada en ensure_session()
    week_start = st.session_state.get("_active_week_start", get_current_week_start())
    save_crane_table(crane_table, week_start=week_start, user_id="default")


def split_into_shifts(start_date, hours, start_shift="T1"):
    """Split a duration starting at the given shift on start_date into (date, shift) hours.

    Shift windows (per user request):
      - T1: 08:00 - 15:30
      - T2: 15:30 - 23:00
      - T3: 23:00 - 06:00 (next day)

    Returns dict with keys like ('2026-02-03', 1) -> hours (hours that fall inside that shift window).
    The duration starts at the selected shift start on `start_date` and continues for `hours`.
    """
    # Start at the selected shift start on the given date
    shift_start_map = {
        "T1": datetime.time(8, 0),
        "T2": datetime.time(15, 30),
        "T3": datetime.time(23, 0),
    }
    start_time = shift_start_map.get(start_shift, datetime.time(8, 0))
    start_dt = datetime.datetime.combine(start_date, start_time)
    end_dt = start_dt + timedelta(hours=hours)
    per_shift = {}

    cur = start_dt
    # iterate day by day, creating three shift windows per day and intersecting
    while cur < end_dt:
        day = cur.date()
        # define shift windows for this day
        t1_start = datetime.datetime.combine(day, datetime.time(8, 0))
        t1_end = datetime.datetime.combine(day, datetime.time(15, 30))
        t2_start = t1_end
        t2_end = datetime.datetime.combine(day, datetime.time(23, 0))
        t3_start = t2_end
        t3_end = datetime.datetime.combine(day + timedelta(days=1), datetime.time(6, 0))

        windows = [
            (t1_start, t1_end, 1),
            (t2_start, t2_end, 2),
            (t3_start, t3_end, 3),
        ]

        for win_start, win_end, shift_num in windows:
            if win_end <= cur:
                continue
            seg_start = max(cur, win_start)
            seg_end = min(end_dt, win_end)
            if seg_end <= seg_start:
                continue
            seg_hours = (seg_end - seg_start).total_seconds() / 3600.0
            key = (str(win_start.date()), shift_num)
            per_shift[key] = per_shift.get(key, 0) + seg_hours
            # advance cur
            if seg_end >= end_dt:
                cur = end_dt
                break
            cur = seg_end
        # if no window matched (e.g., cur in gap between 6:00 and 8:00), advance to next window start
        if cur < end_dt:
            # move to next day's T1 start if in a gap
            next_day_t1 = datetime.datetime.combine((cur + timedelta(days=1)).date(), datetime.time(8, 0))
            if next_day_t1 > cur:
                cur = next_day_t1
    return per_shift


def _shift_window_bounds(day, shift_num):
    """Return (start_dt, end_dt) for a shift number on a given date."""
    if shift_num == 1:
        start_dt = datetime.datetime.combine(day, datetime.time(8, 0))
        end_dt = datetime.datetime.combine(day, datetime.time(15, 30))
    elif shift_num == 2:
        start_dt = datetime.datetime.combine(day, datetime.time(15, 30))
        end_dt = datetime.datetime.combine(day, datetime.time(23, 0))
    else:
        start_dt = datetime.datetime.combine(day, datetime.time(23, 0))
        end_dt = datetime.datetime.combine(day + timedelta(days=1), datetime.time(6, 0))
    return start_dt, end_dt


def build_shift_sequence(start_date, start_shift, num_turns):
    """Build a sequence of (date, shift_num) pairs for a given number of turns."""
    shift_map = {"T1": 1, "T2": 2, "T3": 3}
    shift_num = shift_map.get(start_shift, 1)
    day = start_date
    seq = []
    for _ in range(num_turns):
        seq.append((day, shift_num))
        # advance to next shift
        if shift_num == 1:
            shift_num = 2
        elif shift_num == 2:
            shift_num = 3
        else:
            shift_num = 1
            day = day + timedelta(days=1)
    return seq


def compute_crane_table_from_df(records, edited_df):
    total_movs_by_buque = {}
    for r in records:
        total_movs_by_buque[r.get("buque")] = float(r.get("movimientos", 0) or 0)

    crane_calculated = []
    last_remaining = {}
    for _, row in edited_df.iterrows():
        buque_name = row.get("Buque", "")
        sts = float(row.get("STS", 0) or 0)
        mhc = float(row.get("MHC", 0) or 0)
        rend_sts = float(row.get("Rend. STS", 0) or 0)
        rend_mhc = float(row.get("Rend. MHC", 0) or 0)
        turnos = float(row.get("Turnos", 0) or 0)

        def safe_num(val):
            try:
                if val is None or (isinstance(val, float) and math.isnan(val)):
                    return 0
                return float(val)
            except Exception:
                return 0

        rate_sts = safe_num(sts) * safe_num(rend_sts)
        rate_mhc = safe_num(mhc) * safe_num(rend_mhc)
        total_rate = rate_sts + rate_mhc
        turnos_val = safe_num(turnos)
        if total_rate > 0 and turnos_val > 0:
            movs = math.floor(total_rate * turnos_val * 6.3)
        else:
            movs = 0

        if buque_name not in last_remaining:
            last_remaining[buque_name] = int(total_movs_by_buque.get(buque_name, 0))
        remaining = last_remaining[buque_name] - movs
        last_remaining[buque_name] = remaining

        crane_calculated.append({
            "Buque": buque_name,
            "STS": int(sts),
            "Rend. STS": float(rend_sts),
            "MHC": int(mhc),
            "Rend. MHC": float(rend_mhc),
            "inicio": row.get("inicio", ""),
            "Turnos": int(turnos),
            "Movimientos": int(movs),
            "Movimientos restantes": int(remaining),
        })
    return crane_calculated



def compute_duration_and_turns(record, overrides=None):
    """Compute total duration (hours) and turn count for a record.
    """
    if overrides:
        ov = overrides.get(record.get("buque"))
        if ov and ov.get("duration") is not None and ov.get("turnos") is not None:
            return ov["duration"], ov["turnos"]
    movimientos = float(record.get("movimientos", 0) or 0)
    rendimiento = float(record.get("rendimiento", 0) or 0)
    # Use general rendimiento
    if rendimiento > 0:
        duration = movimientos / rendimiento
        turns = math.ceil(duration / 6.3) if duration > 0 else 0
        return duration, turns

    return 0.0, 0


def records_to_dataframe(records, overrides=None):
    """Convierte registros en DataFrame con filas por (fecha, shift) dentro
    de la semana del inicio del registro.

    Utiliza el rendimiento general del registro para estimar turnos.
    """
    rows = []
    for r in records:
        # determine effective rendimiento por hora
        duration, turnos = compute_duration_and_turns(r, overrides=overrides)
        seq = build_shift_sequence(r["fecha_inicio"], r.get("turno_inicio", "T1"), turnos)
        for day, shift in seq:
            start_dt, end_dt = _shift_window_bounds(day, shift)
            hrs = (end_dt - start_dt).total_seconds() / 3600.0
            rows.append({
                "linea": r["linea"],
                "servicio": r["servicio"],
                "buque": r["buque"],
                "fecha": day.isoformat(),
                "shift": shift,
                "horas": hrs,
                "orig_fecha": r["fecha_inicio"].isoformat(),
            })
    if not rows:
        return pd.DataFrame(columns=["linea", "servicio", "buque", "fecha", "shift", "horas", "orig_fecha", "weekday"])
    df = pd.DataFrame(rows)
    df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
    df["orig_fecha"] = pd.to_datetime(df["orig_fecha"]).dt.date
    df["weekday"] = pd.to_datetime(df["fecha"]).dt.day_name()
    return df


def build_turn_options_for_record(rec, days=7):
    start_date = rec.get("fecha_inicio")
    start_shift = rec.get("turno_inicio", "T1")
    shift_order = ["T1", "T2", "T3"]
    start_idx = shift_order.index(start_shift) if start_shift in shift_order else 0
    options = []
    for d in range(days):
        day = start_date + timedelta(days=d)
        for s in range(3):
            if d == 0 and s < start_idx:
                continue
            turno = shift_order[s]
            options.append(f"{turno}-{day.day:02d}")
    return options


# Initialize session state for records if it doesn't exist
if "records" not in st.session_state:
    st.session_state.records = []


def main():
    st.set_page_config(page_title="Planificación Ventanas", layout="wide")
    st.title("Planificación Ventanas")
    ensure_session()

    # Reset sidebar inputs safely (before widgets are created)
    if st.session_state.get("reset_form", False):
        st.session_state.pop("servicio_select", None)
        st.session_state.pop("linea_select", None)
        st.session_state.pop("buque_input", None)
        st.session_state.pop("rend_input", None)
        st.session_state.pop("mov_input", None)
        st.session_state.pop("fecha_input", None)
        st.session_state.pop("turno_inicio_input", None)
        st.session_state["reset_form"] = False

    # Sidebar: filtros y registro
    st.sidebar.header("Filtros / Registro")
    # Servicio select (expuesto fuera de any form so it updates immediately)
    servicio = st.sidebar.selectbox("Servicio", options=list(LINE_SERVICES.keys()), key="servicio_select")
    linea_options = LINE_SERVICES.get(servicio, [])
    linea = st.sidebar.selectbox("Línea naviera", options=linea_options, key="linea_select")
    buque = st.sidebar.text_input("Nombre del buque", key="buque_input")
    rendimiento = st.sidebar.number_input("Rendimiento (movimientos por hora)", min_value=0.1, value=20.0, step=0.1, key="rend_input")
    movimientos = st.sidebar.number_input("Movimientos (total)", min_value=0, value=0, step=1, key="mov_input")
    
    fecha_inicio = st.sidebar.date_input("Fecha de inicio", key="fecha_input")
    turno_inicio = st.sidebar.selectbox("Turno de inicio", options=["T1", "T2", "T3"], index=0, key="turno_inicio_input")

    # Botones de acción: agregar / borrar registros — mostrados inmediatamente en el formulario
    if st.sidebar.button("Agregar registro"):
        st.session_state.records.append({
            "linea": linea,
            "servicio": servicio,
            "buque": buque or "(sin nombre)",
            "rendimiento": float(rendimiento) if rendimiento is not None else 0.0,
            "movimientos": int(movimientos),
            "fecha_inicio": fecha_inicio,
            "turno_inicio": turno_inicio,
        })
        # Guardar en BD
        save_records_session(st.session_state.records)
        st.sidebar.success("Registro agregado")
        # Reset sidebar filters/inputs on next run
        st.session_state["reset_form"] = True
        st.session_state.pop("selected_turn", None)
        st.session_state.pop("turno_selector", None)
        safe_rerun()

    if st.sidebar.button("Borrar registros"):
        st.session_state.records = []
        st.session_state.pop("selected_turn", None)
        st.session_state.pop("turno_selector", None)
        st.session_state.pop("crane_table", None)
        # Guardar cambios en BD
        save_records_session([])
        save_crane_table_session([])
        safe_rerun()


    # --- Editar registros existentes ---
    st.sidebar.divider()
    st.sidebar.subheader("Editar registros")
    
    if st.session_state.records:
        # Mostrar lista de registros con opción de editar/eliminar
        for idx, record in enumerate(st.session_state.records):
            col1, col2, col3 = st.sidebar.columns([3, 1, 1])
            with col1:
                st.text(f"{record['buque']} ({record['linea']}) - {record['movimientos']} mov")
            with col2:
                if st.button("✏️", key=f"edit_{idx}"):
                    st.session_state["editing_idx"] = idx
            with col3:
                if st.button("🗑️", key=f"delete_{idx}"):
                    st.session_state.records.pop(idx)
                    st.session_state.pop("editing_idx", None)
                    safe_rerun()
        
        # Si se selecciona editar, mostrar formulario de edición
        editing_idx = st.session_state.get("editing_idx")
        if editing_idx is not None and 0 <= editing_idx < len(st.session_state.records):
            st.sidebar.divider()
            record = st.session_state.records[editing_idx]
            st.sidebar.write(f"**Editando:** {record['buque']}")
            
            # Campos editables
            linea = st.sidebar.selectbox(
                "Línea naviera",
                options=["ML", "HL", "ONE", "MSC", "HMM", "TM"],
                index=["ML", "HL", "ONE", "MSC", "HMM", "TM"].index(record['linea']) if record['linea'] in ["ML", "HL", "ONE", "MSC", "HMM", "TM"] else 0,
                key=f"edit_linea_{editing_idx}"
            )
            buque = st.sidebar.text_input("Nombre del buque", value=record['buque'], key=f"edit_buque_{editing_idx}")
            movimientos = st.sidebar.number_input("Movimientos (total)", min_value=0, value=record['movimientos'], step=1, key=f"edit_mov_{editing_idx}")
            rendimiento = st.sidebar.number_input(
                "Rendimiento (movimientos por hora)",
                min_value=0.1,
                value=record['rendimiento'],
                step=0.1,
                key=f"edit_rend_{editing_idx}"
            )
            
            fecha_inicio = st.sidebar.date_input("Fecha de inicio", value=record['fecha_inicio'], key=f"edit_fecha_{editing_idx}")
            turno_inicio = st.sidebar.selectbox("Turno de inicio", options=["T1", "T2", "T3"], index=["T1", "T2", "T3"].index(record['turno_inicio']), key=f"edit_turno_{editing_idx}")
            
            col_save, col_cancel = st.sidebar.columns(2)
            with col_save:
                if st.button("Guardar", key=f"save_{editing_idx}"):
                    st.session_state.records[editing_idx] = {
                        "linea": linea,
                        "servicio": record['servicio'],  # Keep original servicio
                        "buque": buque or "(sin nombre)",
                        "rendimiento": float(rendimiento),
                        "movimientos": int(movimientos),
                        "fecha_inicio": fecha_inicio,
                        "turno_inicio": turno_inicio,
                    }
                    st.session_state.pop("editing_idx", None)
                    st.session_state.pop("selected_turn", None)
                    st.session_state.pop("turno_selector", None)
                    safe_rerun()
            with col_cancel:
                if st.button("Cancelar", key=f"cancel_{editing_idx}"):
                    st.session_state.pop("editing_idx", None)
                    st.rerun()
    else:
        st.sidebar.caption("No hay registros para editar")

    # --- Asignar Grúas eliminado ---

    # Main: Cronograma (primero, vacío si no hay datos)
    records = st.session_state.records
    if not records:
        st.info("Cronograma vacío. Agrega registros desde la barra lateral.")
    else:
        # Mantener el cronograma base usando rendimiento y movimientos del registro
        overrides = {}

        # Calculate current week (Monday to Sunday)
        today = datetime.date.today()
        current_week_start = today - timedelta(days=today.weekday())  # Monday of current week
        
        # Week selector with previous/next buttons
        if "week_offset" not in st.session_state:
            st.session_state.week_offset = 0
        
        col1, col2, col3 = st.columns([1, 4, 1])
        with col1:
            if st.button("◀ Semana anterior"):
                st.session_state.week_offset -= 1
                st.rerun()
        with col3:
            if st.button("Semana siguiente ▶"):
                st.session_state.week_offset += 1
                st.rerun()
        
        # Usar la semana activa que ya fue calculada en ensure_session()
        week_offset = st.session_state.week_offset
        selected_week_start = current_week_start + timedelta(weeks=week_offset)
        selected_week_end = selected_week_start + timedelta(days=6)
        
        st.info(f"Mostrando semana {selected_week_start.isocalendar()[1]}")
        
        # Build Gantt-like dataframe: start (datetime) and end (datetime)
        # Build timeline dynamically, turn by turn, using crane assignments when present
        gantt_rows = []

        # Preparar asignaciones de grúas por buque y turno
        def safe_num(val):
            try:
                if val is None or (isinstance(val, float) and math.isnan(val)):
                    return 0
                return float(val)
            except Exception:
                return 0

        def parse_inicio_value(inicio_val, default_year):
            if not inicio_val:
                return None, None
            try:
                turno_txt, fecha_txt = str(inicio_val).split("-")
                dia, mes = map(int, fecha_txt.split("/"))
                shift_map = {"T1": 1, "T2": 2, "T3": 3}
                shift_num = shift_map.get(turno_txt, None)
                if shift_num is None:
                    return None, None
                start_date = datetime.date(default_year, mes, dia)
                return start_date, shift_num
            except Exception:
                return None, None

        # Construir mapa de asignación de grúas por turno (buque, fecha, turno)
        turn_to_crane_by_key = {}
        for row in st.session_state.get("crane_table", []):
            buque_name = row.get("Buque", "")
            if not buque_name:
                continue
            inicio_val = row.get("inicio", "")
            turnos = int(row.get("Turnos", 0) or 0)
            if turnos <= 0:
                continue

            # Año por defecto: del primer registro del buque si existe
            reg_for_year = next((r for r in records if r.get("buque") == buque_name), None)
            default_year = reg_for_year.get("fecha_inicio").year if reg_for_year else datetime.date.today().year
            start_date, start_shift_num = parse_inicio_value(inicio_val, default_year)
            if not start_date or not start_shift_num:
                continue
            start_shift = {1: "T1", 2: "T2", 3: "T3"}.get(start_shift_num, "T1")

            seq = build_shift_sequence(start_date, start_shift, turnos)
            for day, shift_num in seq:
                turn_to_crane_by_key[(buque_name, day, shift_num)] = row

        # Generar turnos dinámicamente por registro (buque + fecha_inicio)
        for reg in records:
            buque_name = reg.get("buque", "")
            if not buque_name:
                continue
            start_date = reg.get("fecha_inicio", None)
            if not start_date:
                continue
            start_shift = reg.get("turno_inicio", "T1")
            shift_map = {"T1": 1, "T2": 2, "T3": 3}
            shift_num = shift_map.get(start_shift, 1)

            total_moves = int(reg.get("movimientos", 0) or 0)
            remaining = float(total_moves)
            eff_rend = float(reg.get("rendimiento", 0) or 0)

            day = start_date
            safety = 0
            while remaining > 0 and safety < 2000:
                start_dt, end_dt = _shift_window_bounds(day, shift_num)
                hours = (end_dt - start_dt).total_seconds() / 3600.0

                # Calcular rendimiento del turno según asignación de grúas
                key = (buque_name, day, shift_num)
                if key in turn_to_crane_by_key:
                    crane_row = turn_to_crane_by_key[key]
                    sts = float(crane_row.get("STS", 0) or 0)
                    mhc = float(crane_row.get("MHC", 0) or 0)
                    rend_sts = float(crane_row.get("Rend. STS", 0) or 0)
                    rend_mhc = float(crane_row.get("Rend. MHC", 0) or 0)
                    turno_rend = sts * rend_sts + mhc * rend_mhc
                else:
                    turno_rend = eff_rend

                # Capacidad del turno
                capacity = math.floor(turno_rend * 6.3)
                if capacity <= 0:
                    break

                mov_turno = min(remaining, capacity)
                if hours > 1e-6 and mov_turno > 0:
                    if key in turn_to_crane_by_key:
                        sts_lbl = int(turn_to_crane_by_key[key].get("STS", 0) or 0)
                        mhc_lbl = int(turn_to_crane_by_key[key].get("MHC", 0) or 0)
                        mov_label = f"{int(mov_turno)} ({sts_lbl}S, {mhc_lbl}M)"
                    else:
                        mov_label = f"{int(mov_turno)}"

                    turn_label = f"T{shift_num}-{day.strftime('%d/%m') if hasattr(day, 'strftime') else str(day)}"
                    gantt_rows.append({
                        "buque": buque_name,
                        "linea": reg.get("linea", ""),
                        "start": start_dt,
                        "end": end_dt,
                        "fecha": day,
                        "shift": shift_num,
                        "turn_label": turn_label,
                        "horas": hours,
                        "movimientos": None,
                        "mov_per_turn": int(mov_turno),
                        "mov_label": mov_label,
                    })

                remaining -= mov_turno

                # avanzar al siguiente turno
                if shift_num == 1:
                    shift_num = 2
                elif shift_num == 2:
                    shift_num = 3
                else:
                    shift_num = 1
                    day = day + timedelta(days=1)

                safety += 1
        df_gantt = pd.DataFrame(gantt_rows)

        # Filter to only rows within the selected week
        if not df_gantt.empty:
            df_gantt["start_date"] = df_gantt["start"].dt.date
            df_gantt = df_gantt[(df_gantt["start_date"] >= selected_week_start) & (df_gantt["start_date"] <= selected_week_end)].copy()
            df_gantt = df_gantt.drop("start_date", axis=1)
            df_gantt = df_gantt.reset_index(drop=True)
            df_gantt["row_id"] = df_gantt.index.astype(int)

        # Use plotly express timeline for segmented Gantt chart
        if df_gantt.empty:
            st.info("No hay datos en la semana seleccionada.")
        else:
            fig = px.timeline(
                df_gantt,
                x_start="start",
                x_end="end",
                y="buque",
                color="linea",
                color_discrete_map={
                    "HL": "#F97316",      # naranjo
                    "ONE": "#EC4899",     # rosado
                    "HMM": "#1E3A8A",     # azul oscuro
                    "ML": "#60A5FA",      # azul claro
                    "MSC": "#FACC15",     # amarillo
                    "TM": "#14B8A6",      # turquesa
                },
                hover_data={"horas": True, "mov_per_turn": True},
                custom_data=["row_id", "turn_label", "buque"],
                text="mov_label",
            )
            fig.update_yaxes(autorange="reversed")
            
            # Configure text to show inside bars
            fig.update_traces(
                textposition="inside",
                textfont=dict(size=16, color="white", family="Arial, sans-serif"),
                insidetextanchor="middle",
                cliponaxis=False,
                texttemplate='<b>%{text}</b>',
            )
            
            # Calculate dynamic height based on number of unique ships
            num_ships = df_gantt["buque"].nunique()
            bar_height = 120  # Height per ship in pixels
            chart_height = max(600, num_ships * bar_height + 200)  # Min 600px, add 200 for margins/title
            
            fig.update_layout(
                title="Cronograma",
                xaxis_title="Fecha",
                yaxis_title="Buque",
                legend_title_text="Línea",
                margin=dict(l=120, r=20, t=60, b=100),
                height=chart_height,
                template="plotly_dark",
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                clickmode="event+select",
                bargap=0.05,
                uniformtext=dict(mode="show", minsize=8),
            )
            # Date formatting and gridlines
            # Force x-axis range to show full week (Monday to Sunday)
            week_start_dt = datetime.datetime.combine(selected_week_start, datetime.time(0, 0))
            week_end_dt = datetime.datetime.combine(selected_week_end, datetime.time(23, 59))
            
            # Create custom ticks for all 7 days of the week
            all_week_days = [selected_week_start + timedelta(days=i) for i in range(7)]
            tickvals = [datetime.datetime.combine(d, datetime.time(0, 0)) for d in all_week_days]
            ticktext = [d.strftime("%a<br>%d-%m") for d in all_week_days]
            
            fig.update_xaxes(
                showgrid=True,
                gridwidth=1,
                range=[week_start_dt, week_end_dt],
                tickvals=tickvals,
                ticktext=ticktext
            )

            if "selected_turn" not in st.session_state:
                st.session_state.selected_turn = None

            if plotly_events is None:
                st.plotly_chart(fig, use_container_width=True)
                if plotly_events_import_error:
                    st.info(f"No se pudo importar streamlit-plotly-events: {plotly_events_import_error}. Reinicia la app después de instalar.")
                else:
                    st.info("Para habilitar selección por clic en el cronograma, instala 'streamlit-plotly-events' y reinicia la app.")
                selected_points = []
            else:
                selected_points = plotly_events(
                    fig,
                    click_event=True,
                    hover_event=False,
                    select_event=False,
                    override_height=600,
                    key="gantt_click",
                )

            def resolve_selection(point, gantt_df, trace_customdata):
                if not isinstance(point, dict):
                    return None
                custom = point.get("customdata", [])
                if isinstance(custom, (list, tuple)) and len(custom) >= 3:
                    row_id = custom[0]
                    match = gantt_df[gantt_df["row_id"] == int(row_id)]
                    if match.empty:
                        return None
                    row = match.iloc[0]
                    return {"turn_label": row.get("turn_label"), "buque": row.get("buque")}

                curve = point.get("curveNumber")
                pidx = point.get("pointIndex")
                if curve is not None and pidx is not None and curve in trace_customdata:
                    try:
                        cdata = trace_customdata[curve]
                        row_id = cdata[pidx][0]
                        match = gantt_df[gantt_df["row_id"] == int(row_id)]
                        if match.empty:
                            return None
                        row = match.iloc[0]
                        return {"turn_label": row.get("turn_label"), "buque": row.get("buque")}
                    except Exception:
                        pass

                # fallback: use y (buque) and x (start) to find row
                y_val = point.get("y")
                x_val = point.get("x")
                if y_val is None or x_val is None:
                    return None

                df_match = gantt_df.copy()
                df_match["_start_str"] = df_match["start"].astype(str)
                x_str = str(x_val)
                match = df_match[(df_match["buque"] == y_val) & (df_match["_start_str"] == x_str)]
                if match.empty:
                    return None
                row = match.iloc[0]
                return {"turn_label": row.get("turn_label"), "buque": row.get("buque")}

            trace_customdata = {}
            for idx, trace in enumerate(fig.data):
                if hasattr(trace, "customdata"):
                    trace_customdata[idx] = trace.customdata

            if selected_points:
                point = selected_points[0]
                custom = point.get("customdata", []) if isinstance(point, dict) else []
                st.session_state["_last_click_custom"] = custom
                resolved = resolve_selection(point, df_gantt, trace_customdata)
                if resolved:
                    st.session_state.selected_turn = resolved

            if not st.session_state.selected_turn and not df_gantt.empty:
                st.info("Para asignar grúas, selecciona un turno en el selector siguiente.")
                turn_options = []
                for _, row in df_gantt.sort_values(["start"]).iterrows():
                    label = f"{row['buque']} | {row['turn_label']}"
                    turn_options.append((label, row["buque"], row["turn_label"]))
                selected_label = st.selectbox(
                    "Seleccionar turno",
                    options=[o[0] for o in turn_options],
                    index=None,
                    placeholder="Elige un turno...",
                    key="turno_selector",
                )
                if selected_label is not None:
                    match = next((o for o in turn_options if o[0] == selected_label), None)
                    if match:
                        st.session_state.selected_turn = {
                            "buque": match[1],
                            "turn_label": match[2],
                        }

            # Formulario de asignación desde el bloque seleccionado
            if st.session_state.selected_turn:
                sel_buque = st.session_state.selected_turn.get("buque")
                inicio_label = st.session_state.selected_turn.get("turn_label")
                if not inicio_label:
                    st.warning("No se pudo leer el turno seleccionado. Intenta hacer clic otra vez o usa el selector.")
                    st.session_state.selected_turn = None
                    st.rerun()

                st.subheader("Asignar grúas al turno seleccionado")
                st.caption(f"Buque: {sel_buque} | Turno: {inicio_label}")

                if st.button("Limpiar selección"):
                    st.session_state.selected_turn = None
                    st.rerun()

                with st.form("assign_from_gantt"):
                    sts_val = st.number_input("STS", min_value=0, max_value=2, step=1, value=0)
                    rend_sts_val = st.number_input("Rend. STS", min_value=0.0, step=0.001, value=0.0, format="%.3f")
                    mhc_val = st.number_input("MHC", min_value=0, max_value=3, step=1, value=0)
                    rend_mhc_val = st.number_input("Rend. MHC", min_value=0.0, step=0.001, value=0.0, format="%.3f")
                    turnos_val = st.number_input("Turnos", min_value=1, step=1, value=1)
                    apply_block = st.form_submit_button("Aceptar asignación")

                if apply_block:
                    crane_rows = st.session_state.get("crane_table", [])
                    updated = False
                    for row in crane_rows:
                        if row.get("Buque") == sel_buque and row.get("inicio") == inicio_label:
                            row.update({
                                "STS": int(sts_val),
                                "Rend. STS": float(rend_sts_val),
                                "MHC": int(mhc_val),
                                "Rend. MHC": float(rend_mhc_val),
                                "Turnos": int(turnos_val),
                            })
                            updated = True
                            break
                    if not updated:
                        crane_rows.append({
                            "Buque": sel_buque,
                            "STS": int(sts_val),
                            "Rend. STS": float(rend_sts_val),
                            "MHC": int(mhc_val),
                            "Rend. MHC": float(rend_mhc_val),
                            "inicio": inicio_label,
                            "Turnos": int(turnos_val),
                            "Movimientos": 0,
                            "Movimientos restantes": 0,
                        })

                    # Recalcular movimientos y restantes usando la lógica existente
                    crane_df = pd.DataFrame.from_records(crane_rows)
                    st.session_state["crane_table"] = compute_crane_table_from_df(st.session_state.records, crane_df)
                    # Guardar en BD
                    save_crane_table_session(st.session_state["crane_table"])
                    st.rerun()


# Run the Streamlit app
main()