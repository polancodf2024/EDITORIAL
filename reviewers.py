import streamlit as st
import pandas as pd
import io
import os
import time
from datetime import datetime, date

# ============================================================
# PAGE CONFIGURATION
# ============================================================
st.set_page_config(
    page_title="RandPunkt — Reviewers Management",
    page_icon="randpunkt-favicon.png",
    layout="wide"
)

# ============================================================
# CSV COLUMNS
# ============================================================
COLS_ARTICULOS = ["articulo_id", "titulo", "ruta_pdf", "fecha_envio", "estado"]
COLS_REVISORES = [
    "articulo_id", "titulo", "nombre_revisor", "email_revisor",
    "fecha_envio_revision"
]

# ============================================================
# SFTP LAYER
# ============================================================
def _sftp_connect():
    import paramiko
    rs = st.secrets["remote_server"]
    transport = paramiko.Transport((rs["host"], int(rs["port"])))
    transport.connect(username=rs["user"], password=rs["password"])
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp

def _remote_path(filename: str) -> str:
    rs = st.secrets["remote_server"]
    return f"{rs['dir'].rstrip('/')}/{filename}"

def _cleanup_old_backups(sftp, base_name: str, keep_days: int = 30):
    import re
    try:
        rs = st.secrets["remote_server"]
        remote_dir = rs["dir"].rstrip('/')
        pattern = re.compile(rf"^{re.escape(base_name)}\.(\d{{8}}_\d{{6}})\.bak$")
        cutoff = time.time() - keep_days * 86400
        for f in sftp.listdir_attr(remote_dir):
            m = pattern.match(f.filename)
            if m:
                try:
                    ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").timestamp()
                    if ts < cutoff:
                        sftp.remove(f"{remote_dir}/{f.filename}")
                except Exception:
                    pass
    except Exception:
        pass

def read_remote_csv(filename: str, columns: list) -> pd.DataFrame:
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(filename)
        try:
            with sftp.open(path, "r") as f:
                content = f.read().decode("utf-8")
            df = pd.read_csv(io.StringIO(content), dtype=str).fillna("")
            for c in columns:
                if c not in df.columns:
                    df[c] = ""
            df = df[columns]
        except FileNotFoundError:
            df = pd.DataFrame(columns=columns)
        sftp.close()
        transport.close()
        return df
    except Exception as e:
        st.error(f"Error reading {filename}: {e}")
        return pd.DataFrame(columns=columns)

def save_remote_csv(filename: str, df: pd.DataFrame) -> bool:
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(filename)
        tmp_path = f"{path}.tmp"
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{path}.{timestamp}.bak"
            with sftp.open(path, "r") as src:
                previous_content = src.read()
            with sftp.open(backup_path, "w") as dst:
                dst.write(previous_content)
        except IOError:
            pass
        new_content = df.to_csv(index=False).encode("utf-8")
        with sftp.open(tmp_path, "w") as f:
            f.write(new_content)
        try:
            sftp.remove(path)
        except IOError:
            pass
        sftp.rename(tmp_path, path)
        base_name = os.path.basename(filename)
        _cleanup_old_backups(sftp, base_name, keep_days=30)
        sftp.close()
        transport.close()
        return True
    except Exception as e:
        st.error(f"Error saving {filename}: {e}")
        return False

def read_articles() -> pd.DataFrame:
    return read_remote_csv(st.secrets["csv"]["articulos_file"], COLS_ARTICULOS)

def save_articles(df: pd.DataFrame) -> bool:
    return save_remote_csv(st.secrets["csv"]["articulos_file"], df)

def read_reviewers() -> pd.DataFrame:
    return read_remote_csv(st.secrets["csv"]["revisores_file"], COLS_REVISORES)

def save_reviewers(df: pd.DataFrame) -> bool:
    return save_remote_csv(st.secrets["csv"]["revisores_file"], df)

# ============================================================
# HELPERS
# ============================================================
def days_since(iso_date_str: str) -> int:
    if not iso_date_str:
        return -1
    try:
        d = datetime.fromisoformat(iso_date_str).date()
        return (date.today() - d).days
    except Exception:
        return -1

def format_days(dias: int) -> str:
    if dias < 0:
        return "—"
    if dias == 0:
        return "hoy"
    if dias == 1:
        return "1 día"
    return f"{dias} días"

def next_article_id() -> str:
    df = read_articles()
    if df.empty:
        return "ART-0001"
    try:
        max_num = max(int(x.split("-")[1]) for x in df["articulo_id"])
        return f"ART-{max_num + 1:04d}"
    except Exception:
        return "ART-0001"

def unique_reviewers(df_rev: pd.DataFrame) -> pd.DataFrame:
    if df_rev.empty:
        return pd.DataFrame(
            columns=["nombre_revisor", "email_revisor", "asignaciones"]
        )
    grouped = (
        df_rev.groupby(["nombre_revisor", "email_revisor"], as_index=False)
        .size()
        .rename(columns={"size": "asignaciones"})
    )
    return grouped.sort_values("nombre_revisor").reset_index(drop=True)

def download_csv_button(df: pd.DataFrame, filename: str, label: str):
    """Render a download button for the given DataFrame."""
    if df.empty:
        return
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label=label,
        data=csv_bytes,
        file_name=filename,
        mime="text/csv",
        use_container_width=False,
    )

# ============================================================
# EDITOR AUTH
# ============================================================
def authenticate_editor() -> bool:
    if "editor_authenticated" not in st.session_state:
        st.session_state.editor_authenticated = False
    if st.session_state.editor_authenticated:
        return True

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.title("🔒 Editor Panel")
        st.caption("RandPunkt Journal — Reviewers management")
        with st.form("editor_login"):
            password = st.text_input("Editor password", type="password")
            if st.form_submit_button("Sign in", use_container_width=True):
                if password == st.secrets["app"]["editor_password"]:
                    st.session_state.editor_authenticated = True
                    st.rerun()
                else:
                    st.error("Incorrect password.")
    return False

# ============================================================
# TAB 1: ARTICLES
# ============================================================
def tab_articles():
    st.subheader("📄 Artículos")
    st.caption(
        "Catálogo maestro de artículos (registro_articulos.csv). "
        "Se comparte con coauthors.py. Al borrar un artículo aquí, también "
        "se borran sus asignaciones en registro_revisores.csv."
    )

    df_art = read_articles()

    # ---------- ADD ----------
    with st.expander("➕ Añadir artículo", expanded=False):
        with st.form("add_article_form"):
            new_id = st.text_input(
                "ID del artículo",
                value=next_article_id(),
                help="Puedes cambiarlo manualmente si lo necesitas."
            )
            new_title = st.text_input("Título")
            new_pdf = st.text_input("URL pública del PDF (opcional)")
            new_date = st.date_input("Fecha de envío", value=date.today())
            new_estado = st.selectbox(
                "Estado",
                ["pendiente_aprobacion", "todos_aprobados", "publicado"],
                index=0
            )
            submitted = st.form_submit_button(
                "➕ Añadir", use_container_width=True, type="primary"
            )

        if submitted:
            if not new_id.strip() or not new_title.strip():
                st.error("ID y título son obligatorios.")
            elif new_id.strip() in df_art["articulo_id"].values:
                st.error(f"El ID '{new_id.strip()}' ya existe.")
            else:
                new_row = {
                    "articulo_id": new_id.strip(),
                    "titulo": new_title.strip(),
                    "ruta_pdf": new_pdf.strip(),
                    "fecha_envio": datetime.combine(
                        new_date, datetime.min.time()
                    ).isoformat(timespec="seconds"),
                    "estado": new_estado,
                }
                df_art = pd.concat(
                    [df_art, pd.DataFrame([new_row])], ignore_index=True
                )
                if save_articles(df_art):
                    st.success(f"Artículo {new_id} añadido.")
                    st.rerun()

    # ---------- LIST + EDIT + DELETE ----------
    if df_art.empty:
        st.info("No hay artículos registrados.")
        return

    st.markdown(f"### Listado ({len(df_art)} artículos)")
    st.dataframe(df_art, use_container_width=True)
    download_csv_button(
        df_art,
        f"articulos_{datetime.now().strftime('%Y%m%d')}.csv",
        "⬇️ Descargar artículos CSV"
    )

    st.markdown("### Detalle / edición / borrado")
    for idx, row in df_art.iterrows():
        aid = row["articulo_id"]
        header = f"📄 {aid} — {row['titulo']}  |  estado: {row['estado']}"
        with st.expander(header):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(f"**ID:** {aid}")
                st.write(f"**Título:** {row['titulo']}")
                st.write(f"**PDF:** {row['ruta_pdf'] or '—'}")
                st.write(f"**Fecha de envío:** {row['fecha_envio']}")
                st.write(f"**Estado:** `{row['estado']}`")
            with c2:
                if st.button("✏️", key=f"art_edit_{idx}", help="Editar"):
                    st.session_state[f"art_editing_{idx}"] = True

            # --- Edit form ---
            if st.session_state.get(f"art_editing_{idx}", False):
                with st.form(f"art_edit_form_{idx}"):
                    try:
                        current_date = datetime.fromisoformat(
                            row["fecha_envio"]
                        ).date()
                    except Exception:
                        current_date = date.today()

                    e_title = st.text_input(
                        "Título", value=row["titulo"], key=f"a_t_{idx}"
                    )
                    e_pdf = st.text_input(
                        "URL del PDF", value=row["ruta_pdf"], key=f"a_p_{idx}"
                    )
                    e_date = st.date_input(
                        "Fecha de envío", value=current_date, key=f"a_d_{idx}"
                    )
                    e_estado = st.selectbox(
                        "Estado",
                        ["pendiente_aprobacion", "todos_aprobados", "publicado"],
                        index=(
                            ["pendiente_aprobacion", "todos_aprobados", "publicado"]
                            .index(row["estado"])
                            if row["estado"] in
                            ["pendiente_aprobacion", "todos_aprobados", "publicado"]
                            else 0
                        ),
                        key=f"a_e_{idx}"
                    )
                    cs, cc = st.columns(2)
                    with cs:
                        save_btn = st.form_submit_button(
                            "💾 Guardar", use_container_width=True
                        )
                    with cc:
                        cancel_btn = st.form_submit_button(
                            "Cancelar", use_container_width=True
                        )

                    if save_btn:
                        df_art.loc[idx, "titulo"] = e_title.strip()
                        df_art.loc[idx, "ruta_pdf"] = e_pdf.strip()
                        df_art.loc[idx, "fecha_envio"] = datetime.combine(
                            e_date, datetime.min.time()
                        ).isoformat(timespec="seconds")
                        df_art.loc[idx, "estado"] = e_estado
                        if save_articles(df_art):
                            # Also keep titulo in sync inside registro_revisores.csv
                            df_rev = read_reviewers()
                            if not df_rev.empty:
                                df_rev.loc[
                                    df_rev["articulo_id"] == aid, "titulo"
                                ] = e_title.strip()
                                save_reviewers(df_rev)
                            st.session_state[f"art_editing_{idx}"] = False
                            st.success("Artículo actualizado.")
                            st.rerun()
                    if cancel_btn:
                        st.session_state[f"art_editing_{idx}"] = False
                        st.rerun()

            # --- Delete ---
            df_rev = read_reviewers()
            n_assign = (
                (df_rev["articulo_id"] == aid).sum() if not df_rev.empty else 0
            )
            confirm_key = f"art_del_confirm_{idx}"
            if not st.session_state.get(confirm_key, False):
                if st.button(
                    "🗑️ Eliminar artículo",
                    key=f"art_del_{idx}",
                    help=f"También borrará {n_assign} asignación(es) de revisor"
                ):
                    st.session_state[confirm_key] = True
                    st.rerun()
            else:
                st.warning(
                    f"¿Seguro que quieres borrar **{aid}**? "
                    f"Se eliminarán también {n_assign} asignación(es) en "
                    f"registro_revisores.csv."
                )
                cd1, cd2 = st.columns(2)
                with cd1:
                    if st.button(
                        "✅ Sí, borrar",
                        key=f"art_del_yes_{idx}",
                        use_container_width=True
                    ):
                        df_art = df_art.drop(index=idx).reset_index(drop=True)
                        save_articles(df_art)
                        if not df_rev.empty:
                            df_rev = df_rev[
                                df_rev["articulo_id"] != aid
                            ].reset_index(drop=True)
                            save_reviewers(df_rev)
                        st.session_state[confirm_key] = False
                        st.success(f"Artículo {aid} eliminado.")
                        st.rerun()
                with cd2:
                    if st.button(
                        "❌ Cancelar",
                        key=f"art_del_no_{idx}",
                        use_container_width=True
                    ):
                        st.session_state[confirm_key] = False
                        st.rerun()

# ============================================================
# TAB 2: REVIEWERS
# ============================================================
def tab_reviewers():
    st.subheader("👤 Revisores")
    st.caption(
        "Un revisor existe mientras tenga al menos una asignación en "
        "registro_revisores.csv. Editar aquí su nombre o email actualiza "
        "TODAS sus asignaciones a la vez. Borrar aquí lo elimina de TODAS "
        "sus asignaciones."
    )

    df_rev = read_reviewers()
    df_uniq = unique_reviewers(df_rev)

    if df_uniq.empty:
        st.info(
            "Aún no hay revisores. Añade uno en la pestaña "
            "'🔗 Asociaciones' asignándole un artículo."
        )
        return

    st.markdown(f"### Listado ({len(df_uniq)} revisores únicos)")
    st.dataframe(df_uniq, use_container_width=True)
    download_csv_button(
        df_uniq,
        f"revisores_{datetime.now().strftime('%Y%m%d')}.csv",
        "⬇️ Descargar revisores CSV"
    )

    st.markdown("### Detalle / edición / borrado")
    for idx, row in df_uniq.iterrows():
        name = row["nombre_revisor"]
        email = row["email_revisor"]
        n = row["asignaciones"]
        header = f"👤 {name} <{email}>  |  {n} asignación(es)"
        with st.expander(header):
            their = df_rev[
                (df_rev["nombre_revisor"] == name) &
                (df_rev["email_revisor"] == email)
            ]
            st.write("**Artículos asignados:**")
            st.dataframe(
                their[["articulo_id", "titulo", "fecha_envio_revision"]],
                use_container_width=True
            )

            c1, c2 = st.columns([4, 1])
            with c2:
                if st.button("✏️", key=f"rev_edit_{idx}", help="Editar"):
                    st.session_state[f"rev_editing_{idx}"] = True

            if st.session_state.get(f"rev_editing_{idx}", False):
                with st.form(f"rev_edit_form_{idx}"):
                    e_name = st.text_input(
                        "Nombre", value=name, key=f"r_n_{idx}"
                    )
                    e_email = st.text_input(
                        "Email", value=email, key=f"r_e_{idx}"
                    )
                    cs, cc = st.columns(2)
                    with cs:
                        save_btn = st.form_submit_button(
                            "💾 Guardar", use_container_width=True
                        )
                    with cc:
                        cancel_btn = st.form_submit_button(
                            "Cancelar", use_container_width=True
                        )

                    if save_btn:
                        if not e_name.strip() or not e_email.strip():
                            st.error("Nombre y email son obligatorios.")
                        else:
                            mask = (
                                (df_rev["nombre_revisor"] == name) &
                                (df_rev["email_revisor"] == email)
                            )
                            df_rev.loc[mask, "nombre_revisor"] = e_name.strip()
                            df_rev.loc[mask, "email_revisor"] = e_email.strip()
                            if save_reviewers(df_rev):
                                st.session_state[f"rev_editing_{idx}"] = False
                                st.success(
                                    "Revisor actualizado en todas sus asignaciones."
                                )
                                st.rerun()
                    if cancel_btn:
                        st.session_state[f"rev_editing_{idx}"] = False
                        st.rerun()

            # --- Delete reviewer from all assignments ---
            confirm_key = f"rev_del_confirm_{idx}"
            if not st.session_state.get(confirm_key, False):
                if st.button(
                    "🗑️ Eliminar revisor de todas sus asignaciones",
                    key=f"rev_del_{idx}"
                ):
                    st.session_state[confirm_key] = True
                    st.rerun()
            else:
                st.warning(
                    f"¿Seguro que quieres eliminar a **{name} <{email}>** "
                    f"de sus {n} asignación(es)?"
                )
                cd1, cd2 = st.columns(2)
                with cd1:
                    if st.button(
                        "✅ Sí, eliminar",
                        key=f"rev_del_yes_{idx}",
                        use_container_width=True
                    ):
                        mask = (
                            (df_rev["nombre_revisor"] == name) &
                            (df_rev["email_revisor"] == email)
                        )
                        df_rev = df_rev[~mask].reset_index(drop=True)
                        save_reviewers(df_rev)
                        st.session_state[confirm_key] = False
                        st.success(f"Revisor {name} eliminado.")
                        st.rerun()
                with cd2:
                    if st.button(
                        "❌ Cancelar",
                        key=f"rev_del_no_{idx}",
                        use_container_width=True
                    ):
                        st.session_state[confirm_key] = False
                        st.rerun()

# ============================================================
# TAB 3: ASSIGNMENTS
# ============================================================
def tab_assignments():
    st.subheader("🔗 Asociaciones artículo ↔ revisor")
    st.caption(
        "Cada fila de registro_revisores.csv es una asignación. Puedes añadir, "
        "editar la fecha o borrar asignaciones individuales."
    )

    df_art = read_articles()
    df_rev = read_reviewers()
    df_uniq = unique_reviewers(df_rev)

    # ---------- ADD ASSIGNMENT ----------
    if df_art.empty:
        st.warning("Primero debes registrar al menos un artículo.")
    else:
        with st.expander("➕ Añadir asignación", expanded=False):
            with st.form("add_assign_form"):
                art_options = {
                    f"{r['articulo_id']} — {r['titulo']}": (r["articulo_id"], r["titulo"])
                    for _, r in df_art.iterrows()
                }
                art_label = st.selectbox("Artículo", list(art_options.keys()))
                art_id, art_title = art_options[art_label]

                reviewer_options = ["— Nuevo revisor —"]
                if not df_uniq.empty:
                    reviewer_options += [
                        f"{r['nombre_revisor']} <{r['email_revisor']}>"
                        for _, r in df_uniq.iterrows()
                    ]
                reviewer_choice = st.selectbox("Revisor", reviewer_options)

                if reviewer_choice == "— Nuevo revisor —":
                    new_name = st.text_input("Nombre del revisor")
                    new_email = st.text_input("Email del revisor")
                else:
                    new_name = ""
                    new_email = ""

                assign_date = st.date_input(
                    "Fecha de envío al revisor", value=date.today()
                )

                submitted = st.form_submit_button(
                    "➕ Añadir asignación",
                    use_container_width=True,
                    type="primary"
                )

            if submitted:
                if reviewer_choice == "— Nuevo revisor —":
                    if not new_name.strip() or not new_email.strip():
                        st.error("Nombre y email del revisor son obligatorios.")
                        return
                    r_name, r_email = new_name.strip(), new_email.strip()
                else:
                    r_name = reviewer_choice.split(" <")[0]
                    r_email = reviewer_choice.split(" <")[1].rstrip(">")

                dup = df_rev[
                    (df_rev["articulo_id"] == art_id) &
                    (df_rev["email_revisor"] == r_email)
                ]
                if not dup.empty:
                    st.error(
                        f"El revisor {r_name} <{r_email}> ya está asignado "
                        f"a {art_id}."
                    )
                    return

                new_row = {
                    "articulo_id": art_id,
                    "titulo": art_title,
                    "nombre_revisor": r_name,
                    "email_revisor": r_email,
                    "fecha_envio_revision": datetime.combine(
                        assign_date, datetime.min.time()
                    ).isoformat(timespec="seconds"),
                }
                df_rev = pd.concat(
                    [df_rev, pd.DataFrame([new_row])], ignore_index=True
                )
                if save_reviewers(df_rev):
                    st.success(f"Asignación añadida: {art_id} → {r_name}.")
                    st.rerun()

    # ---------- LIST ASSIGNMENTS ----------
    st.divider()
    df_rev = read_reviewers()
    if df_rev.empty:
        st.info("No hay asignaciones todavía.")
        return

    show = df_rev.copy()
    show["días"] = show["fecha_envio_revision"].apply(
        lambda s: format_days(days_since(s))
    )
    st.markdown(f"### Asignaciones ({len(df_rev)})")
    st.dataframe(show, use_container_width=True)
    download_csv_button(
        df_rev,
        f"asignaciones_{datetime.now().strftime('%Y%m%d')}.csv",
        "⬇️ Descargar asignaciones CSV"
    )

    st.markdown("### Detalle / edición / borrado")
    for idx, row in df_rev.iterrows():
        dias_txt = format_days(days_since(row["fecha_envio_revision"]))
        header = (
            f"🔗 {row['articulo_id']} — {row['titulo']}  |  "
            f"Revisor: {row['nombre_revisor']}  |  {dias_txt}"
        )
        with st.expander(header):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(f"**Artículo:** {row['articulo_id']} — {row['titulo']}")
                st.write(f"**Revisor:** {row['nombre_revisor']}")
                st.write(f"**Email:** `{row['email_revisor']}`")
                st.write(
                    f"**Fecha de envío:** {row['fecha_envio_revision'][:10]} "
                    f"({dias_txt})"
                )
            with c2:
                if st.button("✏️", key=f"as_edit_{idx}", help="Editar"):
                    st.session_state[f"as_editing_{idx}"] = True

            if st.session_state.get(f"as_editing_{idx}", False):
                with st.form(f"as_edit_form_{idx}"):
                    try:
                        cur_date = datetime.fromisoformat(
                            row["fecha_envio_revision"]
                        ).date()
                    except Exception:
                        cur_date = date.today()

                    e_name = st.text_input(
                        "Nombre del revisor",
                        value=row["nombre_revisor"],
                        key=f"as_n_{idx}"
                    )
                    e_email = st.text_input(
                        "Email del revisor",
                        value=row["email_revisor"],
                        key=f"as_e_{idx}"
                    )
                    e_date = st.date_input(
                        "Fecha de envío",
                        value=cur_date,
                        key=f"as_d_{idx}"
                    )
                    cs, cc = st.columns(2)
                    with cs:
                        save_btn = st.form_submit_button(
                            "💾 Guardar", use_container_width=True
                        )
                    with cc:
                        cancel_btn = st.form_submit_button(
                            "Cancelar", use_container_width=True
                        )

                    if save_btn:
                        if not e_name.strip() or not e_email.strip():
                            st.error("Nombre y email son obligatorios.")
                        else:
                            df_rev.loc[idx, "nombre_revisor"] = e_name.strip()
                            df_rev.loc[idx, "email_revisor"] = e_email.strip()
                            df_rev.loc[idx, "fecha_envio_revision"] = (
                                datetime.combine(
                                    e_date, datetime.min.time()
                                ).isoformat(timespec="seconds")
                            )
                            if save_reviewers(df_rev):
                                st.session_state[f"as_editing_{idx}"] = False
                                st.success("Asignación actualizada.")
                                st.rerun()
                    if cancel_btn:
                        st.session_state[f"as_editing_{idx}"] = False
                        st.rerun()

            # --- Delete individual assignment ---
            if st.button(
                "🗑️ Eliminar esta asignación",
                key=f"as_del_{idx}"
            ):
                df_rev = df_rev.drop(index=idx).reset_index(drop=True)
                if save_reviewers(df_rev):
                    st.success("Asignación eliminada.")
                    st.rerun()

# ============================================================
# MAIN
# ============================================================
def main():
    if not authenticate_editor():
        return

    with st.sidebar:
        try:
            st.image("randpunkt-logo.png", use_container_width=True)
        except Exception:
            st.markdown("### RandPunkt Journal")
        st.caption("Reviewers management")
        st.divider()
        st.write(f"**Editor:** {st.secrets['email']['sender_name']}")
        st.divider()
        if st.button("🔒 Sign out", use_container_width=True):
            st.session_state.editor_authenticated = False
            st.rerun()

    st.title("🧑‍⚖️ Reviewers Management")

    tab1, tab2, tab3 = st.tabs([
        "📄 Artículos",
        "👤 Revisores",
        "🔗 Asociaciones"
    ])

    with tab1:
        tab_articles()
    with tab2:
        tab_reviewers()
    with tab3:
        tab_assignments()

if __name__ == "__main__":
    main()
