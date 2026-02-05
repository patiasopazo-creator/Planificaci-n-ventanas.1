import streamlit as st
import pandas as pd
import datetime
from datetime import timedelta, date
import plotly.express as px
import plotly.graph_objects as go
import time
import math


def safe_rerun():
    """Try to rerun the Streamlit script. If `st.experimental_rerun` is not available,
    fallback to toggling a session_state key and stop.
    """
    try:
        # Preferred method
        st.experimental_rerun()
    except Exception:
        # Fallback: toggle a session key to force UI update without using
        # deprecated query-params experimental API.
        st.session_state["_refresh"] = not st.session_state.get("_refresh", False)
        # Stop execution now; the UI should refresh on next interaction
        st.stop()

# Mapeo: servicio -> líneas
LINE_SERVICES = {
    "Atacama": ["ML"],
    "WS3": ["HL"],
    "NAN": ["ML", "HL"],
    "AN1": ["HL", "ONE", "MSC", "HMM"],
    "LLAMA": ["MSC"],
    "Cabotaje": ["TM"],
}


def ensure_session():
    if "records" not in st.session_state:
        st.session_state.records = []


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


def compute_duration_and_turns(record, assignments):
    """Compute total duration (hours) and turn count for a record.

    If crane assignments exist for the buque, movements per turn are computed as:
      sum(rend_gruas_in_turn) * 6.3 for each turn except the last, and the last
      turn duration is remaining_movs / sum(rend_gruas_in_last_turn).
    Otherwise, duration = movimientos / rendimiento.
    """
    movimientos = float(record.get("movimientos", 0) or 0)
    rendimiento = float(record.get("rendimiento", 0) or 0)
    buque = record.get("buque")

    cad = assignments.get(buque, {}) if assignments else {}
    # Build per-shift total rend from assignments
    shift_rend = {}
    for v in cad.values():
        if not isinstance(v, dict):
            continue
        rend_val = float(v.get("rend", 0) or 0)
        for s in v.get("shifts", []) or []:
            try:
                idx = int(s)
            except Exception:
                continue
            shift_rend[idx] = shift_rend.get(idx, 0.0) + rend_val

    shift_order = sorted([idx for idx, rv in shift_rend.items() if rv > 0])

    if shift_order:
        remaining = movimientos
        turns = len(shift_order)
        if turns == 1:
            last_rend = shift_rend[shift_order[-1]]
            duration = (remaining / last_rend) if last_rend > 0 else 0.0
            return duration, turns

        # all full turns except last
        duration = 0.0
        for idx in shift_order[:-1]:
            cap = shift_rend[idx] * 6.3
            remaining = max(0.0, remaining - cap)
            duration += 6.3

        last_rend = shift_rend[shift_order[-1]]
        duration += (remaining / last_rend) if last_rend > 0 else 0.0
        return duration, turns

    # Fallback: use general rendimiento
    if rendimiento > 0:
        duration = movimientos / rendimiento
        turns = math.ceil(duration / 6.3) if duration > 0 else 0
        return duration, turns

    return 0.0, 0


def get_avg_rendimiento_per_turn(record, assignments):
    """Compute average rendimiento per turn from crane assignments.
    
    For each assigned turn, sum the rendimientos of all grúas in that turn.
    Then return the average across all turns.
    """
    buque = record.get("buque")
    cad = assignments.get(buque, {}) if assignments else {}
    
    # Build per-turn total rend
    turn_rends = {}
    for v in cad.values():
        if not isinstance(v, dict):
            continue
        rend_val = float(v.get("rend", 0) or 0)
        for s in v.get("shifts", []) or []:
            try:
                idx = int(s)
            except Exception:
                continue
            turn_rends[idx] = turn_rends.get(idx, 0.0) + rend_val
    
    if turn_rends:
        avg = sum(turn_rends.values()) / len(turn_rends)
        return avg
    return float(record.get("rendimiento", 0) or 0)


def records_to_dataframe(records, assignments=None):
    """Convierte registros en DataFrame con filas por (fecha, shift) dentro
    de la semana del inicio del registro.

    Si el registro no tiene `rendimiento` (>0), se intenta usar las
    `assignments` (mapping buque -> {grua: {'shifts': [...], 'rend': ...}})
    para estimar un `rendimiento` efectivo por hora sumando los rendimientos
    de las grúas asignadas a ese buque.
    """
    rows = []
    for r in records:
        # determine effective rendimiento por hora
        duration, turnos = compute_duration_and_turns(r, assignments)
        buque = r.get("buque")
        cad = assignments.get(buque, {}) if assignments else {}

        # If there are assigned shift indices, use them to keep gaps
        shift_indices = []
        for v in cad.values():
            if isinstance(v, dict):
                shift_indices.extend(v.get("shifts", []) or [])
        shift_indices = sorted({int(s) for s in shift_indices if str(s).isdigit()})

        if shift_indices:
            seq = []
            for idx in shift_indices:
                day_offset = (idx - 1) // 3
                shift_num = (idx - 1) % 3 + 1
                day = r["fecha_inicio"] + timedelta(days=day_offset)
                seq.append((day, shift_num))
        else:
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
        return pd.DataFrame(columns=["linea", "servicio", "buque", "fecha", "shift", "horas", "weekday"])
    df = pd.DataFrame(rows)
    df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
    df["orig_fecha"] = pd.to_datetime(df["orig_fecha"]).dt.date
    df["weekday"] = pd.to_datetime(df["fecha"]).dt.day_name()
    return df


def main():
    st.set_page_config(page_title="Planificación Ventanas", layout="wide")
    st.title("Planificación Ventanas")
    ensure_session()

    # Reset sidebar inputs safely (before widgets are created)
    if st.session_state.get("reset_form", False):
        st.session_state["servicio_select"] = list(LINE_SERVICES.keys())[0]
        st.session_state["linea_select"] = LINE_SERVICES.get(st.session_state["servicio_select"], [""])[0]
        st.session_state["buque_input"] = ""
        st.session_state["rend_mode"] = "Asignar rendimiento general"
        st.session_state["rend_input"] = 20.0
        st.session_state["mov_input"] = 0
        st.session_state["fecha_input"] = datetime.date.today()
        st.session_state["turno_inicio_input"] = "T1"
        st.session_state["reset_form"] = False

    # Sidebar: filtros y registro
    st.sidebar.header("Filtros / Registro")
    # Servicio select (expuesto fuera de any form so it updates immediately)
    servicio = st.sidebar.selectbox("Servicio", options=list(LINE_SERVICES.keys()), key="servicio_select")
    linea_options = LINE_SERVICES.get(servicio, [])
    linea = st.sidebar.selectbox("Línea naviera", options=linea_options, key="linea_select")
    buque = st.sidebar.text_input("Nombre del buque", key="buque_input")
    # Rendimientos: modo general o asignar por grúas
    rendimiento_mode = st.sidebar.selectbox("Rendimientos (movimientos por hora)", options=["Asignar rendimiento general", "Asignar grúas"], key="rend_mode")
    if rendimiento_mode == "Asignar rendimiento general":
        rendimiento = st.sidebar.number_input("Rendimiento (movimientos por hora)", min_value=0.1, value=20.0, step=0.1, key="rend_input")
    else:
        rendimiento = None
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
        safe_rerun()

    if st.sidebar.button("Borrar registros"):
        st.session_state.records = []
        safe_rerun()

    # --- Asignar Grúas en sidebar (solo si el modo es 'Asignar grúas') ---
    if rendimiento_mode == "Asignar grúas":
        st.sidebar.markdown("---")
        st.sidebar.subheader("Asignar Grúas")
        # initialize assignments dict (keyed by buque name)
        if "crane_assignments" not in st.session_state:
            st.session_state["crane_assignments"] = {}

        # Use the `buque` currently entered in the sidebar as target for assignments
        if not buque:
            st.sidebar.info("Ingrese el nombre del buque en el formulario para asignar grúas.")
        else:
            target_buque_sb = buque

            cranes = ["STS 1", "STS 2", "MHC 1", "MHC 2", "MHC 3"]
            assign_sb = st.session_state["crane_assignments"].get(target_buque_sb, {})

            def key_for(buque_name, crane, field):
                return f"assign_{buque_name.replace(' ', '_')}_{crane.replace(' ', '')}_{field}"

            selector_key = key_for(target_buque_sb, "selector", "choice")
            selected_crane = st.sidebar.selectbox("Seleccionar grúa", options=cranes, index=0, key=selector_key)

            crane_data_sb = assign_sb.copy() if assign_sb else {}
            crane = selected_crane
            rows_key = key_for(target_buque_sb, crane, "rows")
            rend_key = key_for(target_buque_sb, crane, "rend")

            # Sync saved assignments when switching cranes
            last_crane_key = key_for(target_buque_sb, "selector", "last")
            if st.session_state.get(last_crane_key) != selected_crane:
                saved = assign_sb.get(crane, {}) if assign_sb else {}
                if isinstance(saved, dict) and "shifts" in saved:
                    saved_shifts = sorted(list(set(saved.get("shifts", []))))
                else:
                    saved_shifts = []
                st.session_state[rows_key] = saved_shifts
                st.session_state[rend_key] = float(saved.get("rend", 0) or 0.0) if isinstance(saved, dict) else 0.0
                st.session_state[last_crane_key] = selected_crane

            # New: store absolute shift indices relative to `fecha_inicio`.
            # Build shift labels for a 7-day window starting at `fecha_inicio`.
            def build_shift_options(start_date, days=7, start_shift="T1"):
                labels = []
                idx = 1
                start_shift_num = {"T1": 1, "T2": 2, "T3": 3}.get(start_shift, 1)
                for d in range(days):
                    day = start_date + timedelta(days=d)
                    day_label = day.strftime("%d-%m-%Y")
                    for s, name in enumerate(["T1", "T2", "T3"], start=1):
                        if d == 0 and s < start_shift_num:
                            idx += 1
                            continue
                        labels.append((idx, f"{idx} ({day_label} - {name})", day, name))
                        idx += 1
                return labels

            def get_blocked_slots(exclude_buque, exclude_fecha):
                blocked = set()
                assignments_all = st.session_state.get("crane_assignments", {})
                for rec in st.session_state.records:
                    if rec.get("buque") == exclude_buque and rec.get("fecha_inicio") == exclude_fecha:
                        continue
                    rec_buque = rec.get("buque")
                    rec_fecha = rec.get("fecha_inicio")
                    cad_other = assignments_all.get(rec_buque, {})
                    for v in cad_other.values():
                        if not isinstance(v, dict):
                            continue
                        for s in v.get("shifts", []) or []:
                            try:
                                idx = int(s)
                            except Exception:
                                continue
                            day_offset = (idx - 1) // 3
                            shift_num = (idx - 1) % 3 + 1
                            day = rec_fecha + timedelta(days=day_offset)
                            shift_name = {1: "T1", 2: "T2", 3: "T3"}.get(shift_num, "T1")
                            blocked.add((day, shift_name))
                return blocked

            # initialize rows_key as a list of absolute shift indices
            if rows_key not in st.session_state:
                init_shifts = []
                if assign_sb and assign_sb.get(crane):
                    existing = assign_sb.get(crane)
                    # new format: stored as {'shifts': [1,4,5], 'rend': ...}
                    if isinstance(existing, dict) and "shifts" in existing:
                        init_shifts = list(existing.get("shifts", []))
                    # old format: 'assignments' was a list of {date, shifts:[T1,...]}
                    elif isinstance(existing, dict) and "assignments" in existing:
                        for row in existing.get("assignments", []):
                            row_date = row.get("date")
                            row_shifts = row.get("shifts", [])
                            try:
                                days_diff = (row_date - fecha_inicio).days
                            except Exception:
                                days_diff = 0
                            for sh in row_shifts:
                                shift_num = {"T1": 1, "T2": 2, "T3": 3}.get(sh, None)
                                if shift_num is None:
                                    continue
                                abs_idx = days_diff * 3 + shift_num
                                if abs_idx >= 1:
                                    init_shifts.append(abs_idx)
                # default empty selection
                st.session_state[rows_key] = sorted(list(set(init_shifts)))

            if rend_key not in st.session_state:
                pre_rend = assign_sb.get(crane, {}).get("rend") if assign_sb else 0
                st.session_state[rend_key] = float(pre_rend or 0.0)

            st.sidebar.markdown(f"**{crane}**")
            # Rendimiento (show above turnos)
            rend_val = st.sidebar.number_input("Rendimiento (grúa)", min_value=0.0, value=float(st.session_state.get(rend_key, 0.0) or 0.0), step=0.1, key=rend_key)
            # build options for the week
            shift_options = build_shift_options(fecha_inicio, days=7, start_shift=turno_inicio)
            blocked_slots = get_blocked_slots(target_buque_sb, fecha_inicio)
            # default selections (use current session values)
            default_sel = [str(i) for i in st.session_state.get(rows_key, [])]
            option_values = []
            option_labels = {}
            for idx, label, day, shift_name in shift_options:
                slot = (day, shift_name)
                if slot in blocked_slots and str(idx) not in default_sel:
                    continue
                option_values.append(str(idx))
                option_labels[str(idx)] = label
            widget_key = f"{rows_key}_widget"
            selected = st.sidebar.multiselect("Turnos (índices relativos)", options=option_values, format_func=lambda x: option_labels[x], default=default_sel, key=widget_key)
            # convert to integers locally (don't overwrite the session key used by widgets)
            selected_ints = [int(x) for x in selected]

            # Validation: require movimientos and rendimiento > 0 to allow assigning turns
            # Use movimientos from existing record if available
            existing_record = next((r for r in st.session_state.records if r.get("buque") == target_buque_sb), None)
            movimientos_total = int(existing_record.get("movimientos", movimientos) if existing_record else movimientos)

            def _crane_shifts_count(v):
                # v can be new format {'shifts': [...], 'rend': ...}
                if isinstance(v, dict) and "shifts" in v:
                    return len(v.get("shifts", []))
                # old format: 'assignments' list of {date, shifts:[T1,..]}
                if isinstance(v, dict) and "assignments" in v:
                    return sum(len(a.get("shifts", [])) for a in v.get("assignments", []))
                return 0

            # compute assigned capacity excluding current crane
            assigned_excl = 0.0
            existing_assigns = assign_sb if assign_sb else {}
            for k, v in existing_assigns.items():
                if k == crane:
                    continue
                assigned_excl += v.get("rend", 0) * _crane_shifts_count(v) * 6.3

            potential_per_turn = float(rend_val) * 6.3 if float(rend_val) > 0 else 0.0
            current_selected_capacity = potential_per_turn * len(selected_ints)
            total_if_selected = assigned_excl + current_selected_capacity

            # compute remaining movements and maximum turns allowed for this crane
            if movimientos_total > 0 and potential_per_turn > 0:
                remaining_after_others = max(0.0, movimientos_total - assigned_excl)
                max_total_turns = math.ceil(remaining_after_others / potential_per_turn) if remaining_after_others > 0 else 0
                remaining_after_selection = max(0.0, movimientos_total - assigned_excl - current_selected_capacity)
                additional_turns_allowed = max(0, max_total_turns - len(selected_ints))
            else:
                remaining_after_others = 0.0
                remaining_after_selection = 0.0
                max_total_turns = 0
                additional_turns_allowed = 0

            # Calculate the maximum capacity this crane could handle
            max_capacity_for_crane = max_total_turns * potential_per_turn if potential_per_turn > 0 else 0

            if movimientos_total <= 0:
                st.sidebar.warning("Ingrese la cantidad de movimientos antes de asignar turnos.")
            if float(rend_val) <= 0:
                st.sidebar.warning("Ingrese un rendimiento (>0) para esta grúa antes de asignar turnos.")

            st.sidebar.info(
                f"Turnos seleccionados: {len(selected_ints)}. "
                f"Movimientos restantes: {remaining_after_selection:.0f}. "
                f"Máx. turnos totales para esta grúa: {max_total_turns} "
                f"(adicionales: {additional_turns_allowed}). "
                f"Capacidad máxima: {max_capacity_for_crane:.0f} movimientos"
            )

            crane_data_sb[crane] = {"shifts": selected_ints, "rend": float(rend_val)}

            if st.sidebar.button("Guardar Asignación de Grúas"):
                # Validate before saving
                if movimientos_total <= 0:
                    st.sidebar.error("No se puede guardar: ingresa Movimientos > 0.")
                elif float(rend_val) <= 0:
                    st.sidebar.error("No se puede guardar: ingresa Rendimiento > 0 para la grúa seleccionada.")
                else:
                    # Merge: update only the current crane without overwriting others
                    current_assignments = st.session_state["crane_assignments"].setdefault(target_buque_sb, {})
                    current_assignments[crane] = {"shifts": selected_ints, "rend": float(rend_val)}
                    st.session_state[rows_key] = sorted(list(set(selected_ints)))
                    if total_if_selected > movimientos_total + 1e-6:
                        st.sidebar.warning("Se exceden los movimientos totales, pero la asignación se guarda para poder completar el buque.")
                    else:
                        st.sidebar.success(f"Asignaciones guardadas para {target_buque_sb}")
                    safe_rerun()

            # Totals display removed per request

    # Main: Cronograma (primero, vacío si no hay datos)
    records = st.session_state.records
    if not records:
        st.info("Cronograma vacío. Agrega registros desde la barra lateral.")
    else:
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
        # collect assignments dict (used to compute per-shift movements)
        assignments = st.session_state.get("crane_assignments", {})

        # Build timeline segmented by shifts using records_to_dataframe
        df_shifts = records_to_dataframe(records, assignments)
        gantt_rows = []

        # Group shifts by originating registro (buque + orig_fecha) so we can
        # compute per-shift movements and make the last shift carry the remainder.
        grouped = df_shifts.groupby(["buque", "orig_fecha"])
        for (buque_name, orig_fecha), group in grouped:
            g = group.sort_values(["fecha", "shift"]).reset_index(drop=True)
            # find the original registro for totals
            reg = next((r for r in records if r["buque"] == buque_name and r["fecha_inicio"] == orig_fecha), None)
            total_moves = reg.get("movimientos", 0) if reg else 0
            remaining = float(total_moves)
            cad = assignments.get(buque_name, {})

            # First pass: collect all movements floored (except we'll adjust the last one)
            movements_list = []
            remaining_temp = float(total_moves)

            for idx in range(len(g)):
                row = g.loc[idx]
                date = row["fecha"]
                shift = int(row["shift"])

                # absolute index for this shift relative to registro
                try:
                    abs_idx = (row["fecha"] - reg["fecha_inicio"]).days * 3 + int(row["shift"])
                except Exception:
                    abs_idx = None

                # capacity for this shift: sum assigned cranes rend * 6.3
                shift_capacity = 0.0
                if abs_idx is not None:
                    for v in cad.values():
                        if isinstance(v, dict) and abs_idx in v.get("shifts", []):
                            shift_capacity += float(v.get("rend", 0)) * 6.3

                # fallback: use registro rendimiento if no assigned capacity
                if shift_capacity == 0.0:
                    shift_capacity = float(reg.get("rendimiento", 0)) * 6.3 if reg else 0.0

                # determine movement assigned to this shift
                is_last = (idx == len(g) - 1)
                if not is_last:
                    mov_assigned = min(shift_capacity, max(0.0, remaining_temp))
                    mov_floored = math.floor(mov_assigned)
                else:
                    # last shift: everything that remains (after flooring previous)
                    mov_floored = max(0, int(remaining_temp))

                movements_list.append(mov_floored)
                remaining_temp -= float(mov_assigned) if not is_last else float(mov_floored)

            # Second pass: build gantt_rows with adjusted movements
            sum_floored = 0
            for idx in range(len(g)):
                row = g.loc[idx]
                date = row["fecha"]
                shift = int(row["shift"])
                # shift window bounds
                start_dt, end_dt = _shift_window_bounds(date, shift)
                hours = (end_dt - start_dt).total_seconds() / 3600.0

                is_last = (idx == len(g) - 1)
                mov_floored = movements_list[idx]
                
                # For last shift, adjust to make sum equal total_moves
                if is_last:
                    mov_floored = max(0, int(total_moves) - sum_floored)

                sum_floored += mov_floored

                # skip shifts with no hours or no assigned movements
                if hours <= 1e-6 or mov_floored <= 1e-6:
                    continue

                mov_label = f"{mov_floored}"
                gantt_rows.append({
                    "buque": buque_name,
                    "linea": row.get("linea", ""),
                    "start": start_dt,
                    "end": end_dt,
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

        # Use plotly express timeline for segmented Gantt chart
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
            text="mov_label",
        )
        fig.update_yaxes(autorange="reversed")
        fig.update_layout(
            title="Cronograma",
            xaxis_title="Fecha",
            yaxis_title="Buque",
            legend_title_text="Línea",
            margin=dict(l=120, r=20, t=60, b=100),
            height=600,
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
        # Show text inside bars and center it
        for trace in fig.data:
            trace.textposition = "inside"
            try:
                trace.insidetextanchor = "middle"
            except Exception:
                # older plotly versions may not support insidetextanchor on this trace
                pass

        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    st.subheader("Registros actuales")
    if st.session_state.records:
        records_list = []
        cad = st.session_state.get("crane_assignments", {})
        for r in st.session_state.records:
            has_assign = bool(cad.get(r.get("buque"), {}))
            if has_assign:
                continue
            dur, turnos = compute_duration_and_turns(r, cad)
            # Use average rendimiento per turn if crane assignments exist, otherwise use record rendimiento
            avg_rend = get_avg_rendimiento_per_turn(r, cad)
            records_list.append({
                "Buque": r["buque"],
                "Rendimiento (mov/h)": round(avg_rend, 2),
                "Movimientos": r["movimientos"],
                "Fecha inicio": r["fecha_inicio"].strftime("%d-%m-%Y"),
                "Turno inicio": r.get("turno_inicio", "T1"),
                "Duración (h)": round(dur, 2),
                "Turnos": int(turnos),
            })
        df_records = pd.DataFrame(records_list)
        st.dataframe(df_records)

        # Editar registro: seleccionar, modificar y guardar
        st.markdown("---")
        st.subheader("Editar / Eliminar registro")
        options = [f"{i} - {r['buque']} | {r['servicio']} | {r['linea']} | {r['fecha_inicio'].strftime('%d-%m-%Y')}" for i, r in enumerate(st.session_state.records)]
        sel = st.selectbox("Seleccionar registro", options, key="edit_select")
        try:
            edit_idx = int(sel.split(" - ")[0])
        except Exception:
            edit_idx = 0

        # Prefill editable fields with the selected record
        rec = st.session_state.records[edit_idx]
        cad_all = st.session_state.get("crane_assignments", {})
        has_assignments = rec.get("buque") in cad_all and bool(cad_all.get(rec.get("buque"), {}))
        col1, col2 = st.columns(2)
        with col1:
            new_servicio = st.selectbox(
                "Servicio (editar)",
                options=list(LINE_SERVICES.keys()),
                index=list(LINE_SERVICES.keys()).index(rec.get("servicio")),
                key=f"edit_servicio_{edit_idx}",
            )
            # linea options depend on servicio
            new_linea_options = LINE_SERVICES.get(new_servicio, [])
            new_linea = st.selectbox(
                "Línea naviera (editar)",
                options=new_linea_options,
                index=new_linea_options.index(rec.get("linea")) if rec.get("linea") in new_linea_options else 0,
                key=f"edit_linea_{edit_idx}",
            )
        with col2:
            new_buque = st.text_input("Buque (editar)", value=rec.get("buque"), key=f"edit_buque_{edit_idx}")
            # allow zero rendimiento (assignments may provide capacity), so min_value=0.0
            new_rend = st.number_input(
                "Rendimiento (mov/h) (editar)",
                min_value=0.0,
                value=float(rec.get("rendimiento", 0.0)),
                step=0.1,
                key=f"edit_rend_{edit_idx}",
            )
            new_mov = st.number_input(
                "Movimientos (total) (editar)",
                min_value=0,
                value=int(rec.get("movimientos")),
                step=1,
                key=f"edit_mov_{edit_idx}",
            )
            new_fecha = st.date_input(
                "Fecha de inicio (editar)",
                value=rec.get("fecha_inicio"),
                key=f"edit_fecha_{edit_idx}",
            )
            new_turno = st.selectbox(
                "Turno de inicio (editar)",
                options=["T1", "T2", "T3"],
                index=["T1", "T2", "T3"].index(rec.get("turno_inicio", "T1")),
                key=f"edit_turno_{edit_idx}",
            )
            use_cranes_edit = st.checkbox(
                "Asignar grúas a este buque",
                value=has_assignments,
                key=f"edit_use_cranes_{edit_idx}",
            )

        if st.button("Guardar cambios"):
            # If fecha/turno changed and there are assignments, clear to avoid mismatched dates
            if use_cranes_edit and (
                new_fecha != rec.get("fecha_inicio")
                or new_turno != rec.get("turno_inicio", "T1")
            ):
                if "crane_assignments" in st.session_state:
                    st.session_state["crane_assignments"].pop(rec.get("buque"), None)
                st.warning("Se limpiaron las asignaciones de grúas por cambio de fecha/turno de inicio.")

            st.session_state.records[edit_idx] = {
                "linea": new_linea,
                "servicio": new_servicio,
                "buque": new_buque or "(sin nombre)",
                "rendimiento": float(new_rend),
                "movimientos": int(new_mov),
                "fecha_inicio": new_fecha,
                "turno_inicio": new_turno,
            }
            # If user disabled crane assignments, remove them for this buque
            if not use_cranes_edit:
                if "crane_assignments" in st.session_state:
                    st.session_state["crane_assignments"].pop(rec.get("buque"), None)
            st.success("Registro actualizado")
            safe_rerun()

        if st.button("Eliminar registro", key="del_reg"):
            st.session_state.records.pop(edit_idx)
            st.success("Registro eliminado")
            safe_rerun()

        # --- Editor de Asignaciones de Grúas para el registro seleccionado ---
        # Mostrar sólo si existen asignaciones para este buque (o permitir crearlas)
        if "crane_assignments" not in st.session_state:
            st.session_state["crane_assignments"] = {}

        orig_buque_name = rec.get("buque")
        st.markdown("---")
        st.subheader("Asignaciones de grúas (editar)")
        st.write(f"Buque original: **{orig_buque_name}**")
        if not use_cranes_edit:
            st.info("Activa 'Asignar grúas a este buque' para editar asignaciones.")

        # helper to build shift options relative to this registro's fecha_inicio
        def build_shift_options_edit(start_date, days=7, start_shift="T1"):
            labels = []
            idx = 1
            start_shift_num = {"T1": 1, "T2": 2, "T3": 3}.get(start_shift, 1)
            for d in range(days):
                day = start_date + timedelta(days=d)
                day_label = day.strftime("%d-%m-%Y")
                for s, name in enumerate(["T1", "T2", "T3"], start=1):
                    if d == 0 and s < start_shift_num:
                        idx += 1
                        continue
                    labels.append((idx, f"{idx} ({day_label} - {name})", day, name))
                    idx += 1
            return labels

        def get_blocked_slots_edit(exclude_buque, exclude_fecha):
            blocked = set()
            assignments_all = st.session_state.get("crane_assignments", {})
            for rec in st.session_state.records:
                if rec.get("buque") == exclude_buque and rec.get("fecha_inicio") == exclude_fecha:
                    continue
                rec_buque = rec.get("buque")
                rec_fecha = rec.get("fecha_inicio")
                cad_other = assignments_all.get(rec_buque, {})
                for v in cad_other.values():
                    if not isinstance(v, dict):
                        continue
                    for s in v.get("shifts", []) or []:
                        try:
                            idx = int(s)
                        except Exception:
                            continue
                        day_offset = (idx - 1) // 3
                        shift_num = (idx - 1) % 3 + 1
                        day = rec_fecha + timedelta(days=day_offset)
                        shift_name = {1: "T1", 2: "T2", 3: "T3"}.get(shift_num, "T1")
                        blocked.add((day, shift_name))
            return blocked

        edit_assignments = st.session_state.get("crane_assignments", {}).get(orig_buque_name, {})
        cranes = ["STS 1", "STS 2", "MHC 1", "MHC 2", "MHC 3"]

        # Local structure to hold edits before saving
        updated_assigns = {k: {"shifts": list(v.get("shifts", [])), "rend": float(v.get("rend", 0))} for k, v in edit_assignments.items()} if edit_assignments else {}

        if use_cranes_edit:
            for crane in cranes:
                st.markdown(f"**{crane}**")
                # rendimiento for this crane
                rend_key_edit = f"edit_{edit_idx}_{crane}_rend"
                prev_rend = updated_assigns.get(crane, {}).get("rend", 0.0)
                rend_val_edit = st.number_input(f"Rendimiento (grúa) {crane}", min_value=0.0, value=float(prev_rend or 0.0), step=0.1, key=rend_key_edit)

                # shifts options relative to the original record's fecha/turno inicio (matches assignments)
                start_for_shifts = rec.get("fecha_inicio")
                shift_options = build_shift_options_edit(start_for_shifts, days=7, start_shift=rec.get("turno_inicio", "T1"))
                blocked_slots = get_blocked_slots_edit(orig_buque_name, rec.get("fecha_inicio"))
                prev_sel = [str(i) for i in updated_assigns.get(crane, {}).get("shifts", [])]
                option_values = []
                option_labels = {}
                for idx, label, day, shift_name in shift_options:
                    slot = (day, shift_name)
                    if slot in blocked_slots and str(idx) not in prev_sel:
                        continue
                    option_values.append(str(idx))
                    option_labels[str(idx)] = label
                widget_key_edit = f"edit_{edit_idx}_{crane}_shifts_widget"
                sel = st.multiselect(f"Turnos {crane}", options=option_values, format_func=lambda x: option_labels[x], default=prev_sel, key=widget_key_edit)
                sel_ints = [int(x) for x in sel]

                # remaining movements calculation (edit mode)
                mov_total_edit = int(rec.get("movimientos", 0))
                assigned_excl_edit = 0.0
                for k2, v2 in updated_assigns.items():
                    if k2 == crane:
                        continue
                    if isinstance(v2, dict):
                        assigned_excl_edit += float(v2.get("rend", 0)) * len(v2.get("shifts", [])) * 6.3
                potential_per_turn_edit = float(rend_val_edit) * 6.3 if float(rend_val_edit) > 0 else 0.0
                current_selected_capacity_edit = potential_per_turn_edit * len(sel_ints)
                remaining_after_selection_edit = max(0.0, mov_total_edit - assigned_excl_edit - current_selected_capacity_edit)
                max_total_turns_edit = math.ceil((mov_total_edit - assigned_excl_edit) / potential_per_turn_edit) if potential_per_turn_edit > 0 and mov_total_edit - assigned_excl_edit > 0 else 0
                additional_turns_edit = max(0, max_total_turns_edit - len(sel_ints))

                st.caption(
                    f"Movimientos restantes: {remaining_after_selection_edit:.0f}. "
                    f"Máx. turnos totales: {max_total_turns_edit} (adicionales: {additional_turns_edit})."
                )

                # store locally
                if sel_ints or rend_val_edit > 0:
                    updated_assigns[crane] = {"shifts": sel_ints, "rend": float(rend_val_edit)}
                elif crane in updated_assigns:
                    # remove if now empty
                    updated_assigns.pop(crane, None)

            if st.button("Guardar Asignaciones del buque (editar)"):
                target_name = new_buque or "(sin nombre)"
                # ensure assignments mapping exists
                if "crane_assignments" not in st.session_state:
                    st.session_state["crane_assignments"] = {}

                # If buque name changed, move existing assignments to new key
                if orig_buque_name != target_name and orig_buque_name in st.session_state["crane_assignments"]:
                    st.session_state["crane_assignments"][target_name] = st.session_state["crane_assignments"].pop(orig_buque_name)

                # save updated assigns (may be empty dict)
                st.session_state["crane_assignments"][target_name] = updated_assigns
                st.success(f"Asignaciones guardadas para {target_name}")
                safe_rerun()

        # (Removed) Assign Grúas in main panel — Assign Grúas is managed from the sidebar only now.
    else:
        st.info("No hay registros. Agrega uno desde la barra lateral.")


if __name__ == "__main__":
    main()
