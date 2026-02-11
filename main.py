import streamlit as st
import pandas as pd
import datetime
from datetime import timedelta, date
import plotly.express as px
import plotly.graph_objects as go
import time
import math

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
    if "records" not in st.session_state:
        st.session_state.records = []
    if "crane_table" not in st.session_state:
        st.session_state.crane_table = []


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
        
        week_offset = st.session_state.week_offset
        selected_week_start = current_week_start + timedelta(weeks=week_offset)
        selected_week_end = selected_week_start + timedelta(days=6)
        
        st.info(f"Mostrando semana {selected_week_start.isocalendar()[1]}")
        
        # Build Gantt-like dataframe: start (datetime) and end (datetime)
        # Build timeline segmented by shifts using records_to_dataframe
        df_shifts = records_to_dataframe(records, overrides=None)
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

        # Construir mapa de capacidad por turno desde la tabla de grúas
        crane_capacity_by_turn = {}
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

            sts = safe_num(row.get("STS", 0))
            mhc = safe_num(row.get("MHC", 0))
            rend_sts = safe_num(row.get("Rend. STS", 0))
            rend_mhc = safe_num(row.get("Rend. MHC", 0))
            total_rate = sts * rend_sts + mhc * rend_mhc
            capacity_per_turn = total_rate * 6.3

            seq = build_shift_sequence(start_date, start_shift, turnos)
            for day, shift_num in seq:
                crane_capacity_by_turn[(buque_name, day, shift_num)] = capacity_per_turn

        # Group shifts by originating registro (buque + orig_fecha)
        grouped = df_shifts.groupby(["buque", "orig_fecha"])
        for (buque_name, orig_fecha), group in grouped:
            g = group.sort_values(["fecha", "shift"]).reset_index(drop=True)
            # find the original registro for totals
            reg = next((r for r in records if r["buque"] == buque_name and r["fecha_inicio"] == orig_fecha), None)
            ov = {}
            total_moves = reg.get("movimientos", 0) if reg else 0
            remaining = float(total_moves)
            eff_rend = reg.get("rendimiento", 0) if reg else 0.0

            # Calcular movimientos por turno con nueva lógica
            movements_list = []
            accumulated_moves = 0
            
            # Mapear cada turno con su asignación de grúas (si aplica)
            # crane_table puede tener asignaciones con múltiples turnos consecutivos
            turn_to_crane = {}
            for crane_row in st.session_state.get("crane_table", []):
                if crane_row.get("Buque") != buque_name:
                    continue
                inicio_val = crane_row.get("inicio", "")
                turnos_asignados = int(crane_row.get("Turnos", 0) or 0)
                
                # Encontrar el turno de inicio en g
                start_idx = None
                for idx in range(len(g)):
                    row = g.loc[idx]
                    shift = int(row["shift"])
                    date = row["fecha"]
                    turn_label = f"T{shift}-{date.strftime('%d/%m') if hasattr(date, 'strftime') else str(date)}"
                    if turn_label == inicio_val:
                        start_idx = idx
                        break
                
                # Asignar las grúas a los turnos consecutivos
                if start_idx is not None:
                    for offset in range(turnos_asignados):
                        if start_idx + offset < len(g):
                            turn_to_crane[start_idx + offset] = crane_row

            for idx in range(len(g)):
                row = g.loc[idx]
                date = row["fecha"]
                shift = int(row["shift"])

                # Calcular rendimiento del turno
                if idx in turn_to_crane:
                    # Si hay asignación de grúas, calcular rendimiento desde grúas
                    crane_row = turn_to_crane[idx]
                    sts = float(crane_row.get("STS", 0) or 0)
                    mhc = float(crane_row.get("MHC", 0) or 0)
                    rend_sts = float(crane_row.get("Rend. STS", 0) or 0)
                    rend_mhc = float(crane_row.get("Rend. MHC", 0) or 0)
                    turno_rend = sts * rend_sts + mhc * rend_mhc
                else:
                    # Si no hay asignación, usar rendimiento del registro
                    turno_rend = eff_rend

                # Calcular movimientos del turno: rendimiento × 6.3
                mov_turno = math.floor(turno_rend * 6.3)
                
                # Verificar si este sería el último turno (suma >= total)
                if accumulated_moves + mov_turno >= total_moves:
                    # Último turno: asignar solo el residuo exacto
                    mov_turno = max(0, int(total_moves - accumulated_moves))
                    movements_list.append(mov_turno)
                    break  # Terminar aquí, no agregar más turnos
                
                movements_list.append(mov_turno)
                accumulated_moves += mov_turno

            # Second pass: build gantt_rows with calculated movements
            for idx in range(len(g)):
                row = g.loc[idx]
                date = row["fecha"]
                shift = int(row["shift"])
                start_dt, end_dt = _shift_window_bounds(date, shift)
                hours = (end_dt - start_dt).total_seconds() / 3600.0

                # Usar el valor ya calculado en movements_list
                if idx < len(movements_list):
                    mov_floored = movements_list[idx]
                else:
                    mov_floored = 0

                # Saltar si no hay horas o si no hay movimientos
                if hours <= 1e-6 or mov_floored <= 0:
                    continue

                # Verificar si este turno tiene asignación de grúas
                if idx in turn_to_crane:
                    crane_row = turn_to_crane[idx]
                    sts = int(crane_row.get("STS", 0) or 0)
                    mhc = int(crane_row.get("MHC", 0) or 0)
                    mov_label = f"{mov_floored}\n{sts} STS, {mhc} MHC"
                else:
                    mov_label = f"{mov_floored}"
                
                turn_label = f"T{shift}-{date.strftime('%d/%m') if hasattr(date, 'strftime') else str(date)}"
                gantt_rows.append({
                    "buque": buque_name,
                    "linea": row.get("linea", ""),
                    "start": start_dt,
                    "end": end_dt,
                    "fecha": date,
                    "shift": shift,
                    "turn_label": turn_label,
                    "horas": hours,
                    "movimientos": None,
                    "mov_per_turn": mov_floored,
                    "mov_label": mov_label,
                })
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
            
            # Update traces to show text centered on bars with background
            fig.update_traces(
                textposition="inside",
                textfont=dict(size=20, color="white", family="Arial", weight="bold"),
                insidetextanchor="middle",
            )
            
            fig.update_layout(
                title="Cronograma",
                xaxis_title="Fecha",
                yaxis_title="Buque",
                legend_title_text="Línea",
                margin=dict(l=120, r=20, t=60, b=100),
                height=600,
                template="plotly_dark",
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                clickmode="event+select",
                bargap=0.3,  # Reduce gap between bars to make them thicker (0-1, smaller = thicker bars)
                uniformtext_minsize=8,
                uniformtext_mode='hide',
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
                    st.rerun()


# Run the Streamlit app
main()