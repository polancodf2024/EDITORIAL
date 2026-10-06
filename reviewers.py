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
# OFFICIAL REMOTE FILE NAMES
# ============================================================
ARTICULOS_FILE = "registro_articulos.csv"
REVISIONES_FILE = "registro_revisores.csv"

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
    return read_remote_csv(ARTICULOS_FILE, COLS_ARTICULOS)

def save_articles(df: pd.DataFrame) -> bool:
    return save_remote_csv(ARTICULOS_FILE, df)

def read_reviewers() -> pd.DataFrame:
    return read_remote_csv(REVISIONES_FILE, COLS_REVISORES)

def save_reviewers(df: pd.DataFrame) -> bool:
    return save_remote_csv(REVISIONES_FILE, df)

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
        return "today"
    if dias == 1:
        return "1 day"
    return f"{dias} days"

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
        st.caption("RandPunkt — Reviewers management")
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
    st.subheader("📄 Articles")
    st.caption(
        f"Master catalogue of articles ({ARTICULOS_FILE}). "
        "Shared with coauthors.py. Deleting an article here also removes "
        f"its assignments in {REVISIONES_FILE}."
    )

    df_art = read_articles()

    # ---------- ADD ----------
    with st.expander("➕ Add article", expanded=False):
        with st.form("add_article_form"):
            new_id = st.text_input(
                "Article ID",
                value=next_article_id(),
                help="You can change it manually if needed."
            )
            new_title = st.text_input("Title")
            new_pdf = st.text_input("Public PDF URL (optional)")
            new_date = st.date_input("Submission date", value=date.today())
            new_estado = st.selectbox(
                "Status",
                ["pendiente_aprobacion", "todos_aprobados", "publicado"],
                index=0
            )
            submitted = st.form_submit_button(
                "➕ Add", use_container_width=True, type="primary"
            )

        if submitted:
            if not new_id.strip() or not new_title.strip():
                st.error("ID and title are required.")
            elif new_id.strip() in df_art["articulo_id"].values:
                st.error(f"The ID '{new_id.strip()}' already exists.")
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
                    st.success(f"Article {new_id} added.")
                    st.rerun()

    # ---------- LIST + EDIT + DELETE ----------
    if df_art.empty:
        st.info("No articles registered.")
        return

    st.markdown(f"### List ({len(df_art)} articles)")
    st.dataframe(df_art, use_container_width=True)
    download_csv_button(
        df_art,
        f"articulos_{datetime.now().strftime('%Y%m%d')}.csv",
        "⬇️ Download articles CSV"
    )

    st.markdown("### Detail / edit / delete")
    for idx, row in df_art.iterrows():
        aid = row["articulo_id"]
        header = f"📄 {aid} — {row['titulo']}  |  status: {row['estado']}"
        with st.expander(header):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(f"**ID:** {aid}")
                st.write(f"**Title:** {row['titulo']}")
                st.write(f"**PDF:** {row['ruta_pdf'] or '—'}")
                st.write(f"**Submission date:** {row['fecha_envio']}")
                st.write(f"**Status:** `{row['estado']}`")
            with c2:
                if st.button("✏️", key=f"art_edit_{idx}", help="Edit"):
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
                        "Title", value=row["titulo"], key=f"a_t_{idx}"
                    )
                    e_pdf = st.text_input(
                        "PDF URL", value=row["ruta_pdf"], key=f"a_p_{idx}"
                    )
                    e_date = st.date_input(
                        "Submission date", value=current_date, key=f"a_d_{idx}"
                    )
                    e_estado = st.selectbox(
                        "Status",
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
                            "💾 Save", use_container_width=True
                        )
                    with cc:
                        cancel_btn = st.form_submit_button(
                            "Cancel", use_container_width=True
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
                            st.success("Article updated.")
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
                    "🗑️ Delete article",
                    key=f"art_del_{idx}",
                    help=f"Also deletes {n_assign} reviewer assignment(s)"
                ):
                    st.session_state[confirm_key] = True
                    st.rerun()
            else:
                st.warning(
                    f"Are you sure you want to delete **{aid}**? "
                    f"This will also remove {n_assign} assignment(s) in "
                    f"{REVISIONES_FILE}."
                )
                cd1, cd2 = st.columns(2)
                with cd1:
                    if st.button(
                        "✅ Yes, delete",
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
                        st.success(f"Article {aid} deleted.")
                        st.rerun()
                with cd2:
                    if st.button(
                        "❌ Cancel",
                        key=f"art_del_no_{idx}",
                        use_container_width=True
                    ):
                        st.session_state[confirm_key] = False
                        st.rerun()

# ============================================================
# TAB 2: REVIEWERS
# ============================================================
def tab_reviewers():
    st.subheader("👤 Reviewers")
    st.caption(
        "A reviewer exists as long as they have at least one assignment in "
        f"{REVISIONES_FILE}. Editing their name or email here updates "
        "ALL their assignments at once. Deleting here removes them from ALL "
        "their assignments."
    )

    df_rev = read_reviewers()
    df_uniq = unique_reviewers(df_rev)

    if df_uniq.empty:
        st.info(
            "No reviewers yet. Add one in the "
            "'🔗 Assignments' tab by assigning an article to them."
        )
        return

    st.markdown(f"### List ({len(df_uniq)} unique reviewers)")
    st.dataframe(df_uniq, use_container_width=True)
    download_csv_button(
        df_uniq,
        f"revisores_{datetime.now().strftime('%Y%m%d')}.csv",
        "⬇️ Download reviewers CSV"
    )

    st.markdown("### Detail / edit / delete")
    for idx, row in df_uniq.iterrows():
        name = row["nombre_revisor"]
        email = row["email_revisor"]
        n = row["asignaciones"]
        header = f"👤 {name} <{email}>  |  {n} assignment(s)"
        with st.expander(header):
            their = df_rev[
                (df_rev["nombre_revisor"] == name) &
                (df_rev["email_revisor"] == email)
            ]
            st.write("**Assigned articles:**")
            st.dataframe(
                their[["articulo_id", "titulo", "fecha_envio_revision"]],
                use_container_width=True
            )

            c1, c2 = st.columns([4, 1])
            with c2:
                if st.button("✏️", key=f"rev_edit_{idx}", help="Edit"):
                    st.session_state[f"rev_editing_{idx}"] = True

            if st.session_state.get(f"rev_editing_{idx}", False):
                with st.form(f"rev_edit_form_{idx}"):
                    e_name = st.text_input(
                        "Name", value=name, key=f"r_n_{idx}"
                    )
                    e_email = st.text_input(
                        "Email", value=email, key=f"r_e_{idx}"
                    )
                    cs, cc = st.columns(2)
                    with cs:
                        save_btn = st.form_submit_button(
                            "💾 Save", use_container_width=True
                        )
                    with cc:
                        cancel_btn = st.form_submit_button(
                            "Cancel", use_container_width=True
                        )

                    if save_btn:
                        if not e_name.strip() or not e_email.strip():
                            st.error("Name and email are required.")
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
                                    "Reviewer updated across all assignments."
                                )
                                st.rerun()
                    if cancel_btn:
                        st.session_state[f"rev_editing_{idx}"] = False
                        st.rerun()

            # --- Delete reviewer from all assignments ---
            confirm_key = f"rev_del_confirm_{idx}"
            if not st.session_state.get(confirm_key, False):
                if st.button(
                    "🗑️ Delete reviewer from all assignments",
                    key=f"rev_del_{idx}"
                ):
                    st.session_state[confirm_key] = True
                    st.rerun()
            else:
                st.warning(
                    f"Are you sure you want to delete **{name} <{email}>** "
                    f"from their {n} assignment(s)?"
                )
                cd1, cd2 = st.columns(2)
                with cd1:
                    if st.button(
                        "✅ Yes, delete",
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
                        st.success(f"Reviewer {name} deleted.")
                        st.rerun()
                with cd2:
                    if st.button(
                        "❌ Cancel",
                        key=f"rev_del_no_{idx}",
                        use_container_width=True
                    ):
                        st.session_state[confirm_key] = False
                        st.rerun()

# ============================================================
# TAB 3: ASSIGNMENTS
# ============================================================
def tab_assignments():
    st.subheader("🔗 Article ↔ reviewer assignments")
    st.caption(
        f"Each row in {REVISIONES_FILE} is an assignment. You can add, "
        "edit the date, or delete individual assignments."
    )

    df_art = read_articles()
    df_rev = read_reviewers()
    df_uniq = unique_reviewers(df_rev)

    # ---------- ADD ASSIGNMENT ----------
    if df_art.empty:
        st.warning("You must register at least one article first.")
    else:
        with st.expander("➕ Add assignment", expanded=False):
            with st.form("add_assign_form"):
                art_options = {
                    f"{r['articulo_id']} — {r['titulo']}": (r["articulo_id"], r["titulo"])
                    for _, r in df_art.iterrows()
                }
                art_label = st.selectbox("Article", list(art_options.keys()))
                art_id, art_title = art_options[art_label]

                reviewer_options = ["— New reviewer —"]
                if not df_uniq.empty:
                    reviewer_options += [
                        f"{r['nombre_revisor']} <{r['email_revisor']}>"
                        for _, r in df_uniq.iterrows()
                    ]
                reviewer_choice = st.selectbox("Reviewer", reviewer_options)

                if reviewer_choice == "— New reviewer —":
                    new_name = st.text_input("Reviewer name")
                    new_email = st.text_input("Reviewer email")
                else:
                    new_name = ""
                    new_email = ""

                assign_date = st.date_input(
                    "Date sent to reviewer", value=date.today()
                )

                submitted = st.form_submit_button(
                    "➕ Add assignment",
                    use_container_width=True,
                    type="primary"
                )

            if submitted:
                if reviewer_choice == "— New reviewer —":
                    if not new_name.strip() or not new_email.strip():
                        st.error("Reviewer name and email are required.")
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
                        f"Reviewer {r_name} <{r_email}> is already assigned "
                        f"to {art_id}."
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
                    st.success(f"Assignment added: {art_id} → {r_name}.")
                    st.rerun()

    # ---------- LIST ASSIGNMENTS ----------
    st.divider()
    df_rev = read_reviewers()
    if df_rev.empty:
        st.info("No assignments yet.")
        return

    show = df_rev.copy()
    show["days"] = show["fecha_envio_revision"].apply(
        lambda s: format_days(days_since(s))
    )
    st.markdown(f"### Assignments ({len(df_rev)})")
    st.dataframe(show, use_container_width=True)
    download_csv_button(
        df_rev,
        f"asignaciones_{datetime.now().strftime('%Y%m%d')}.csv",
        "⬇️ Download assignments CSV"
    )

    st.markdown("### Detail / edit / delete")
    for idx, row in df_rev.iterrows():
        dias_txt = format_days(days_since(row["fecha_envio_revision"]))
        header = (
            f"🔗 {row['articulo_id']} — {row['titulo']}  |  "
            f"Reviewer: {row['nombre_revisor']}  |  {dias_txt}"
        )
        with st.expander(header):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(f"**Article:** {row['articulo_id']} — {row['titulo']}")
                st.write(f"**Reviewer:** {row['nombre_revisor']}")
                st.write(f"**Email:** `{row['email_revisor']}`")
                st.write(
                    f"**Date sent:** {row['fecha_envio_revision'][:10]} "
                    f"({dias_txt})"
                )
            with c2:
                if st.button("✏️", key=f"as_edit_{idx}", help="Edit"):
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
                        "Reviewer name",
                        value=row["nombre_revisor"],
                        key=f"as_n_{idx}"
                    )
                    e_email = st.text_input(
                        "Reviewer email",
                        value=row["email_revisor"],
                        key=f"as_e_{idx}"
                    )
                    e_date = st.date_input(
                        "Date sent",
                        value=cur_date,
                        key=f"as_d_{idx}"
                    )
                    cs, cc = st.columns(2)
                    with cs:
                        save_btn = st.form_submit_button(
                            "💾 Save", use_container_width=True
                        )
                    with cc:
                        cancel_btn = st.form_submit_button(
                            "Cancel", use_container_width=True
                        )

                    if save_btn:
                        if not e_name.strip() or not e_email.strip():
                            st.error("Name and email are required.")
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
                                st.success("Assignment updated.")
                                st.rerun()
                    if cancel_btn:
                        st.session_state[f"as_editing_{idx}"] = False
                        st.rerun()

            # --- Delete individual assignment ---
            if st.button(
                "🗑️ Delete this assignment",
                key=f"as_del_{idx}"
            ):
                df_rev = df_rev.drop(index=idx).reset_index(drop=True)
                if save_reviewers(df_rev):
                    st.success("Assignment deleted.")
                    st.rerun()

# ============================================================
# MAIN
# ============================================================
def main():
    if not authenticate_editor():
        return

    with st.sidebar:
        try:
            st.image("randpunkt-logo.png", width=200)
        except Exception:
            st.markdown("### RandPunkt")
        st.caption("Reviewers management")
        st.divider()
        st.write(f"**Editor:** {st.secrets['email']['sender_name']}")
        st.divider()
        if st.button("🔒 Sign out", use_container_width=True):
            st.session_state.editor_authenticated = False
            st.rerun()

    st.title("🧑‍⚖️ Reviewers Management")

    tab1, tab2, tab3 = st.tabs([
        "📄 Articles",
        "👤 Reviewers",
        "🔗 Assignments"
    ])

    with tab1:
        tab_articles()
    with tab2:
        tab_reviewers()
    with tab3:
        tab_assignments()

if __name__ == "__main__":
    main()
