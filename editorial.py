import streamlit as st
import pandas as pd
import smtplib
import io
import os
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature
from datetime import datetime, date

# ============================================================
# PAGE CONFIGURATION
# ============================================================
st.set_page_config(
    page_title="RandPunkt — Approval Management",
    page_icon="randpunkt-favicon.png",
    layout="wide"
)

# ============================================================
# CSV COLUMNS (kept in Spanish to preserve existing files)
# ============================================================
COLS_ARTICULOS = ["articulo_id", "titulo", "ruta_pdf", "fecha_envio", "estado"]
COLS_CONFIRMACIONES = [
    "articulo_id", "nombre", "email", "token",
    "aprobado", "fecha_aprobacion", "comentarios"
]
COLS_CONTACTOS = ["Nombre", "Email"]

# ---- NEW layout for revisiones.csv (5 columns only) ----
COLS_REVISIONES = [
    "articulo_id",
    "titulo",
    "nombre_revisor",
    "email_revisor",
    "fecha_envio_revision",
]

# ============================================================
# SFTP LAYER — ALL PERSISTENCE GOES TO THE REMOTE SERVER
# ============================================================
def _sftp_connect():
    """Open an SFTP connection to the remote server."""
    import paramiko
    rs = st.secrets["remote_server"]
    transport = paramiko.Transport((rs["host"], int(rs["port"])))
    transport.connect(username=rs["user"], password=rs["password"])
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp

def _remote_path(filename: str) -> str:
    rs = st.secrets["remote_server"]
    return f"{rs['dir'].rstrip('/')}/{filename}"

def _ensure_directory(sftp, path):
    """Create a remote directory if it does not exist."""
    try:
        sftp.stat(path)
    except IOError:
        sftp.mkdir(path)

def _cleanup_old_backups(sftp, base_name: str, keep_days: int = 30):
    """Delete backups older than keep_days. Best-effort."""
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
    """Read a CSV from the remote server. If it does not exist, return empty."""
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
        st.error(f"Error reading {filename} from remote server: {e}")
        return pd.DataFrame(columns=columns)

def save_remote_csv(filename: str, df: pd.DataFrame) -> bool:
    """Write a CSV to the remote server atomically, with a timestamped backup."""
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(filename)
        tmp_path = f"{path}.tmp"

        # 1. Backup the previous version (if any)
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{path}.{timestamp}.bak"
            with sftp.open(path, "r") as src:
                previous_content = src.read()
            with sftp.open(backup_path, "w") as dst:
                dst.write(previous_content)
        except IOError:
            pass

        # 2. Write the new version to a temporary file
        new_content = df.to_csv(index=False).encode("utf-8")
        with sftp.open(tmp_path, "w") as f:
            f.write(new_content)

        # 3. Atomic rename over the destination
        try:
            sftp.remove(path)
        except IOError:
            pass
        sftp.rename(tmp_path, path)

        # 4. Clean up old backups (best-effort)
        base_name = os.path.basename(filename)
        _cleanup_old_backups(sftp, base_name, keep_days=30)

        sftp.close()
        transport.close()
        return True
    except Exception as e:
        st.error(f"Error saving {filename} to remote server: {e}")
        return False

def read_articles() -> pd.DataFrame:
    return read_remote_csv(st.secrets["csv"]["articulos_file"], COLS_ARTICULOS)

def read_confirmations() -> pd.DataFrame:
    return read_remote_csv(st.secrets["csv"]["confirmaciones_file"], COLS_CONFIRMACIONES)

def read_revisions() -> pd.DataFrame:
    return read_remote_csv(st.secrets["csv"]["revisiones_file"], COLS_REVISIONES)

def save_articles(df: pd.DataFrame) -> bool:
    return save_remote_csv(st.secrets["csv"]["articulos_file"], df)

def save_confirmations(df: pd.DataFrame) -> bool:
    return save_remote_csv(st.secrets["csv"]["confirmaciones_file"], df)

def save_revisions(df: pd.DataFrame) -> bool:
    return save_remote_csv(st.secrets["csv"]["revisiones_file"], df)

def migrate_revisions_csv():
    """One-shot migration: if the remote revisiones.csv has legacy columns
    (token, estado_revision, dictamen, comentarios_revisor, fecha_respuesta),
    rewrite it with the new 5-column layout. Keeps only the matching rows."""
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(st.secrets["csv"]["revisiones_file"])
        try:
            with sftp.open(path, "r") as f:
                content = f.read().decode("utf-8")
        except FileNotFoundError:
            sftp.close()
            transport.close()
            return

        df = pd.read_csv(io.StringIO(content), dtype=str).fillna("")
        legacy = {"token", "estado_revision", "dictamen",
                  "comentarios_revisor", "fecha_respuesta"}
        if legacy & set(df.columns):
            # Keep only the new columns, in the right order
            for c in COLS_REVISIONES:
                if c not in df.columns:
                    df[c] = ""
            df = df[COLS_REVISIONES]
            new_content = df.to_csv(index=False).encode("utf-8")
            tmp_path = f"{path}.tmp"
            with sftp.open(tmp_path, "w") as f:
                f.write(new_content)
            try:
                sftp.remove(path)
            except IOError:
                pass
            sftp.rename(tmp_path, path)
        sftp.close()
        transport.close()
    except Exception:
        # Best-effort: if migration fails, the app still works with the
        # in-memory projection done by read_remote_csv.
        pass

def read_contacts() -> pd.DataFrame:
    """Read the contacts CSV from the remote server."""
    try:
        transport, sftp = _sftp_connect()
        path = _remote_path(st.secrets["csv"]["contactos_file"])
        try:
            with sftp.open(path, "r") as f:
                content = f.read().decode("utf-8")
            df = pd.read_csv(
                io.StringIO(content),
                dtype=str,
                skipinitialspace=True,
            ).fillna("")
            df.columns = [c.strip() for c in df.columns]
            if "Nombre" not in df.columns or "Email" not in df.columns:
                st.error("The contacts CSV must have 'Nombre' and 'Email' columns.")
                sftp.close()
                transport.close()
                return pd.DataFrame(columns=COLS_CONTACTOS)
            df["Nombre"] = df["Nombre"].str.strip()
            df["Email"] = df["Email"].str.strip()
            df["Email"] = df["Email"].str.replace(r"^<|>$", "", regex=True).str.strip()
            df = df[(df["Nombre"] != "") & (df["Email"] != "")].reset_index(drop=True)
            sftp.close()
            transport.close()
            return df[COLS_CONTACTOS]
        except FileNotFoundError:
            sftp.close()
            transport.close()
            return pd.DataFrame(columns=COLS_CONTACTOS)
    except Exception as e:
        st.error(f"Error reading contacts from remote server: {e}")
        return pd.DataFrame(columns=COLS_CONTACTOS)

def next_article_id() -> str:
    df = read_articles()
    if df.empty:
        return "ART-0001"
    try:
        max_num = max(int(x.split("-")[1]) for x in df["articulo_id"])
        return f"ART-{max_num + 1:04d}"
    except Exception:
        return "ART-0001"

def upload_pdf_sftp(file_bytes: bytes, remote_name: str):
    """Upload a PDF to the remote server inside the manuscripts/ folder."""
    try:
        import paramiko
        rs = st.secrets["remote_server"]
        transport = paramiko.Transport((rs["host"], int(rs["port"])))
        transport.connect(username=rs["user"], password=rs["password"])
        sftp = paramiko.SFTPClient.from_transport(transport)

        manuscripts_dir = f"{rs['dir'].rstrip('/')}/manuscritos"
        _ensure_directory(sftp, manuscripts_dir)

        remote_path = f"{manuscripts_dir}/{remote_name}"
        tmp_path = f"{remote_path}.tmp"

        with sftp.open(tmp_path, "wb") as f:
            f.write(file_bytes)

        try:
            sftp.remove(remote_path)
        except IOError:
            pass
        sftp.rename(tmp_path, remote_path)

        sftp.close()
        transport.close()

        public_url = f"{rs['public_url'].rstrip('/')}/manuscritos/{remote_name}"
        return True, public_url
    except Exception as e:
        return False, str(e)

# ============================================================
# TOKENS (author approvals only)
# ============================================================
def get_serializer():
    return URLSafeTimedSerializer(st.secrets["app"]["secret_key"])

def generate_token(email: str, article_id: str, name: str) -> str:
    s = get_serializer()
    return s.dumps(
        {"email": email, "articulo_id": article_id, "nombre": name},
        salt="aprobacion-autor"
    )

def validate_token(token: str):
    s = get_serializer()
    try:
        data = s.loads(token, salt="aprobacion-autor", max_age=2592000)  # 30 days
        return data, None
    except SignatureExpired:
        return None, "The link has expired (more than 30 days)."
    except BadTimeSignature:
        return None, "The link is invalid or has been tampered with."

# ============================================================
# HTML / PLAIN EMAIL BUILDERS (authors only)
# ============================================================
def build_html_email(name, title, link, article_id, is_reminder=False):
    cfg = st.secrets["app"]
    email_cfg = st.secrets["email"]

    logo_url = cfg["logo_url"]
    journal = cfg["journal_name"]
    series = cfg["journal_series"]
    issn = cfg["journal_issn"]
    editor_name = email_cfg["sender_name"]
    editor_email = email_cfg["notification_email"]

    prefix = "Reminder: " if is_reminder else ""
    intro = (
        "We would like to remind you that we have not yet received your "
        "approval confirmation for the following manuscript. Please review "
        "the link below."
        if is_reminder
        else
        "As part of the editorial process, we kindly ask you to review and "
        "approve the manuscript before its publication."
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{prefix}Manuscript approval</title>
</head>
<body style="margin:0; padding:0; background:#F5F4F0;
             font-family: Georgia, 'Times New Roman', serif;
             color:#1B2A4A; line-height:1.6;">
  <div style="max-width:620px; margin:0 auto; padding:24px 12px;">

    <div style="background:#FFFFFF; padding:24px 28px;
                border-bottom:3px solid #5B7A99; text-align:center;
                border-radius:2px 2px 0 0;">
      <img src="{logo_url}" alt="{journal}"
           style="max-width:240px; height:auto; display:block;
                  margin:0 auto 10px auto;">
      <p style="margin:0; font-size:12px; letter-spacing:0.05em;
                color:#8A8F98; text-transform:uppercase;">
        {series} &nbsp;&bull;&nbsp; ISSN {issn}
      </p>
    </div>

    <div style="background:#FFFFFF; padding:32px 28px;">
      <p style="margin-top:0;">Dear <strong>{name}</strong>,</p>

      <p>{intro}</p>

      <p style="margin:24px 0 8px 0;">Manuscript:</p>
      <p style="margin:0 0 24px 0; padding:12px 16px;
                background:#F5F4F0; border-left:3px solid #B08D57;
                font-style:italic;">
        &ldquo;{title}&rdquo;<br>
        <span style="font-size:12px; color:#8A8F98;
                     font-style:normal;">Reference: {article_id}</span>
      </p>

      <p>To confirm that you have read the manuscript and agree with its
         content, please click the button below:</p>

      <p style="text-align:center; margin:32px 0;">
        <a href="{link}"
           style="display:inline-block; background:#5B7A99; color:#FFFFFF;
                  padding:13px 32px; text-decoration:none;
                  border-radius:2px; font-family: Arial, sans-serif;
                  font-weight:bold; font-size:15px;
                  letter-spacing:0.03em;">
          Confirm approval
        </a>
      </p>

      <p style="font-size:13px; color:#8A8F98; margin-top:24px;">
        The link is personal and non-transferable, and expires in 30 days.
        If you have comments or suggestions, you may include them when
        confirming your approval.
      </p>

      <p style="margin-top:28px;">Best regards,<br>
         <strong>{editor_name}</strong><br>
         <span style="color:#8A8F98; font-size:13px;">{journal}</span>
      </p>
    </div>

    <div style="background:#F5F4F0; padding:16px 28px; font-size:11px;
                color:#8A8F98; text-align:center;
                border-top:1px solid #E8E6E1;
                border-radius:0 0 2px 2px;">
      This email was sent automatically by the editorial system of
      {journal}.<br>
      If you received it in error, please notify
      <a href="mailto:{editor_email}" style="color:#5B7A99;
         text-decoration:none;">{editor_email}</a>.
    </div>

  </div>
</body>
</html>"""


def build_plain_email(name, title, link, article_id, is_reminder=False):
    cfg = st.secrets["app"]
    email_cfg = st.secrets["email"]
    journal = cfg["journal_name"]
    editor_email = email_cfg["notification_email"]
    editor_name = email_cfg["sender_name"]

    prefix = "REMINDER: " if is_reminder else ""
    intro = (
        "We would like to remind you that we have not yet received your "
        "approval confirmation for the following manuscript."
        if is_reminder
        else
        "As part of the editorial process, we kindly ask you to review and "
        "approve the manuscript before its publication."
    )

    return f"""{prefix}Manuscript approval — {journal}

Dear {name},

{intro}

Manuscript: "{title}"
Reference: {article_id}

To confirm that you have read the manuscript and agree with its content,
please visit the following link:

{link}

The link is personal and non-transferable, and expires in 30 days.
If you have comments or suggestions, you may include them when
confirming your approval.

Best regards,
{editor_name}
{journal}

---
This email was sent automatically by the editorial system of {journal}.
If you received it in error, please notify {editor_email}.
"""


def send_email(recipient, name, link, title, article_id,
               is_reminder=False) -> bool:
    try:
        cfg = st.secrets["email"]
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg['sender_name']} <{cfg['sender_email']}>"
        msg["To"] = recipient
        prefix = "Reminder — " if is_reminder else ""
        msg["Subject"] = f"{prefix}[RandPunkt] Approval required: {title}"

        plain = build_plain_email(name, title, link, article_id, is_reminder)
        html = build_html_email(name, title, link, article_id, is_reminder)

        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        server = smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]))
        server.starttls()
        server.login(cfg["sender_email"], cfg["sender_password"])
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        st.error(f"Error sending email to {recipient}: {e}")
        return False


def send_editor_notification(message: str):
    try:
        cfg = st.secrets["email"]
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg['sender_name']} <{cfg['sender_email']}>"
        msg["To"] = cfg["notification_email"]
        msg["Subject"] = "[RandPunkt] All authors have approved a manuscript"
        msg.attach(MIMEText(message, "plain", "utf-8"))

        server = smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]))
        server.starttls()
        server.login(cfg["sender_email"], cfg["sender_password"])
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        st.warning(f"Could not notify the editor: {e}")
        return False

# ============================================================
# HELPERS FOR DATE MATH
# ============================================================
def days_since(iso_date_str: str) -> int:
    """Return the number of days elapsed since the given ISO date string.

    Returns -1 if the date cannot be parsed.
    """
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

# ============================================================
# EDITOR VIEW
# ============================================================
def authenticate_editor() -> bool:
    if "editor_authenticated" not in st.session_state:
        st.session_state.editor_authenticated = False
    if st.session_state.editor_authenticated:
        return True

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.title("🔒 Editor Panel")
        st.caption("RandPunkt Journal — Approval system")
        with st.form("editor_login"):
            password = st.text_input("Editor password", type="password")
            if st.form_submit_button("Sign in", use_container_width=True):
                if password == st.secrets["app"]["editor_password"]:
                    st.session_state.editor_authenticated = True
                    st.rerun()
                else:
                    st.error("Incorrect password.")
    return False


def editor_view():
    with st.sidebar:
        try:
            st.image("randpunkt-logo.png", use_container_width=True)
        except Exception:
            st.markdown("### RandPunkt Journal")
        st.caption("Editorial panel")
        st.divider()
        st.write(f"**Editor:** {st.secrets['email']['sender_name']}")
        st.write(f"**Notifications:** {st.secrets['email']['notification_email']}")
        st.divider()
        if st.button("🔒 Sign out", use_container_width=True):
            st.session_state.editor_authenticated = False
            st.rerun()

    st.title("📚 Editorial Approval Management")

    tab1, tab2, tab3, tab4 = st.tabs([
        "📤 New submission",
        "📋 Status by article",
        "🔍 Peer review",
        "📊 Full log"
    ])

    # ---------- TAB 1: NEW SUBMISSION ----------
    with tab1:
        st.subheader("Register manuscript and request approvals")

        df_contacts = read_contacts()

        if df_contacts.empty:
            st.warning(
                "No contacts found in the remote contacts CSV. "
                f"Please add entries to "
                f"'{st.secrets['csv']['contactos_file']}' "
                f"on the remote server."
            )
        else:
            st.info(
                f"{len(df_contacts)} contact(s) available in "
                f"'{st.secrets['csv']['contactos_file']}'."
            )

        title = st.text_input("Manuscript title")
        pdf_url = st.text_input(
            "Public URL of the PDF (or upload below via SFTP)"
        )

        st.markdown("**Select the authors to notify:**")

        selected = []
        if not df_contacts.empty:
            select_all = st.checkbox("Select all", value=False)

            for i, row in df_contacts.iterrows():
                name = row["Nombre"]
                email = row["Email"]
                key = f"contact_{i}_{email}"

                if select_all:
                    st.session_state[key] = True

                checked = st.checkbox(
                    f"{name}  <{email}>",
                    key=key
                )
                if checked:
                    selected.append((name, email))
        else:
            st.write("(no contacts available)")

        if st.button(
            "📨 Send approval requests",
            use_container_width=True,
            type="primary"
        ):
            if not title:
                st.error("The manuscript title is required.")
            elif not selected:
                st.error("You must select at least one contact.")
            else:
                article_id = next_article_id()
                df_art = read_articles()
                new_row = {
                    "articulo_id": article_id,
                    "titulo": title,
                    "ruta_pdf": pdf_url,
                    "fecha_envio": datetime.now().isoformat(timespec="seconds"),
                    "estado": "pendiente_aprobacion"
                }
                df_art = pd.concat(
                    [df_art, pd.DataFrame([new_row])],
                    ignore_index=True
                )
                save_articles(df_art)

                df_conf = read_confirmations()
                base_url = st.secrets["app"]["base_url"].rstrip("/")
                successes = 0
                for name, email in selected:
                    token = generate_token(email, article_id, name)
                    link = f"{base_url}/?token={token}"
                    if send_email(email, name, link, title, article_id):
                        row = {
                            "articulo_id": article_id,
                            "nombre": name,
                            "email": email,
                            "token": token,
                            "aprobado": "0",
                            "fecha_aprobacion": "",
                            "comentarios": ""
                        }
                        df_conf = pd.concat(
                            [df_conf, pd.DataFrame([row])],
                            ignore_index=True
                        )
                        successes += 1

                save_confirmations(df_conf)
                st.success(
                    f"✅ Article {article_id} registered. "
                    f"Sent {successes}/{len(selected)} emails."
                )

        st.divider()
        st.subheader("📎 Upload PDF to remote server (optional)")
        with st.form("pdf_upload_form"):
            file = st.file_uploader(
                "Select the manuscript PDF", type=["pdf"]
            )
            upload = st.form_submit_button(
                "⬆️ Upload PDF via SFTP", use_container_width=True
            )

        if upload and file is not None:
            with st.spinner("Uploading PDF to remote server..."):
                remote_name = (
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.name}"
                )
                ok, result = upload_pdf_sftp(file.getbuffer(), remote_name)
                if ok:
                    st.success(f"PDF uploaded: {result}")
                    st.info("Copy this URL into the 'Public URL of the PDF' field.")
                else:
                    st.error(f"Upload error: {result}")

    # ---------- TAB 2: STATUS BY ARTICLE ----------
    with tab2:
        st.subheader("Approval status")
        df_art = read_articles()
        df_conf = read_confirmations()

        if df_art.empty:
            st.info("No articles registered yet.")
        else:
            for _, art in df_art.iterrows():
                article_authors = df_conf[df_conf["articulo_id"] == art["articulo_id"]]
                total = len(article_authors)
                approved = (article_authors["aprobado"] == "1").sum() if total else 0

                icon = "✅" if total > 0 and approved == total else "⏳"
                with st.expander(
                    f"{icon} {art['articulo_id']} — {art['titulo']}  "
                    f"({approved}/{total} approved)"
                ):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.write(f"**Status:** `{art['estado']}`")
                        st.write(f"**Submission date:** {art['fecha_envio']}")
                    with col2:
                        if art["ruta_pdf"]:
                            st.markdown(f"**PDF:** [View]({art['ruta_pdf']})")
                        else:
                            st.write("**PDF:** —")

                    if total == 0:
                        st.info("No authors registered.")
                    else:
                        table = article_authors[[
                            "nombre", "email", "aprobado",
                            "fecha_aprobacion", "comentarios"
                        ]].copy()
                        table["aprobado"] = table["aprobado"].map(
                            {"1": "✅ Yes", "0": "⏳ Pending"}
                        )
                        st.dataframe(table, use_container_width=True)

                        pending = article_authors[article_authors["aprobado"] == "0"]
                        if len(pending) > 0:
                            if st.button(
                                f"📧 Send reminder to "
                                f"{len(pending)} pending author(s)",
                                key=f"rem_{art['articulo_id']}"
                            ):
                                base_url = st.secrets["app"]["base_url"].rstrip("/")
                                sent = 0
                                for _, p in pending.iterrows():
                                    link = f"{base_url}/?token={p['token']}"
                                    if send_email(
                                        p["email"], p["nombre"], link,
                                        art["titulo"], art["articulo_id"],
                                        is_reminder=True
                                    ):
                                        sent += 1
                                st.success(f"Reminders sent: {sent}")

                    if total > 0 and approved == total and art["estado"] != "publicado":
                        if st.button(
                            f"🚀 Mark {art['articulo_id']} as published",
                            key=f"pub_{art['articulo_id']}"
                        ):
                            df_art.loc[
                                df_art["articulo_id"] == art["articulo_id"],
                                "estado"
                            ] = "publicado"
                            save_articles(df_art)
                            st.success(f"Article {art['articulo_id']} marked as published.")
                            st.rerun()

    # ---------- TAB 3: PEER REVIEW (lightweight tracking) ----------
    with tab3:
        st.subheader("Peer review tracking")
        st.caption(
            "Asigne revisores a los manuscritos. Copie el email del revisor "
            "y escríbale desde su propio correo. Use ✏️ para editar los datos "
            "y 🗑️ para eliminar la asignación."
        )

        df_art = read_articles()
        df_rev = read_revisions()

        # --- Assign reviewer form ---
        if df_art.empty:
            st.info("You must register at least one article before assigning reviewers.")
        else:
            with st.expander("➕ Asignar revisor a un manuscrito", expanded=False):
                article_options = {
                    f"{row['articulo_id']} — {row['titulo']}": row["articulo_id"]
                    for _, row in df_art.iterrows()
                }
                selected_label = st.selectbox(
                    "Manuscrito", list(article_options.keys()),
                    key="rev_article"
                )
                selected_article_id = article_options[selected_label]
                selected_title = selected_label.split(" — ", 1)[1]

                col_a, col_b, col_c = st.columns([2, 2, 1])
                with col_a:
                    rev_name = st.text_input("Nombre del revisor", key="rev_name")
                with col_b:
                    rev_email = st.text_input("Email del revisor", key="rev_email")
                with col_c:
                    rev_date = st.date_input(
                        "Fecha de envío", value=date.today(), key="rev_date"
                    )

                if st.button(
                    "➕ Agregar revisor",
                    use_container_width=True,
                    type="primary",
                    key="add_reviewer_btn"
                ):
                    if not rev_name.strip() or not rev_email.strip():
                        st.error("Nombre y email del revisor son obligatorios.")
                    else:
                        new_rev = {
                            "articulo_id": selected_article_id,
                            "titulo": selected_title,
                            "nombre_revisor": rev_name.strip(),
                            "email_revisor": rev_email.strip(),
                            "fecha_envio_revision": datetime.combine(
                                rev_date, datetime.min.time()
                            ).isoformat(timespec="seconds")
                        }
                        df_rev = pd.concat(
                            [df_rev, pd.DataFrame([new_rev])],
                            ignore_index=True
                        )
                        save_revisions(df_rev)
                        st.success(
                            f"Revisor {rev_name} agregado a {selected_article_id}."
                        )
                        st.rerun()

        st.divider()
        st.markdown("### 📋 Revisiones asignadas")

        df_rev = read_revisions()
        if df_rev.empty:
            st.info("Aún no hay revisores asignados.")
        else:
            # Compact overview table
            overview = df_rev.copy()
            overview["días"] = overview["fecha_envio_revision"].apply(
                lambda s: format_days(days_since(s))
            )
            st.dataframe(
                overview[[
                    "articulo_id", "titulo", "nombre_revisor",
                    "email_revisor", "fecha_envio_revision", "días"
                ]],
                use_container_width=True
            )

            st.markdown("#### Detalle / edición")
            for idx, row in df_rev.iterrows():
                dias = days_since(row["fecha_envio_revision"])
                dias_txt = format_days(dias)

                header = (
                    f"🔍 {row['articulo_id']} — {row['titulo']}  |  "
                    f"Revisor: {row['nombre_revisor']}  |  {dias_txt}"
                )

                with st.expander(header):
                    c1, c2 = st.columns([4, 1])
                    with c1:
                        st.write(f"**Artículo:** {row['articulo_id']} — {row['titulo']}")
                        st.write(f"**Revisor:** {row['nombre_revisor']}")
                        st.write(f"**Email:** `{row['email_revisor']}`")
                        st.write(
                            f"**Fecha de envío:** "
                            f"{row['fecha_envio_revision'][:10]}  "
                            f"({dias_txt} con el manuscrito)"
                        )
                    with c2:
                        if st.button("✏️", key=f"edit_{idx}", help="Editar"):
                            st.session_state[f"editing_{idx}"] = True

                    if st.session_state.get(f"editing_{idx}", False):
                        with st.form(f"edit_form_{idx}"):
                            try:
                                current_date = datetime.fromisoformat(
                                    row["fecha_envio_revision"]
                                ).date()
                            except Exception:
                                current_date = date.today()

                            new_date = st.date_input(
                                "Nueva fecha de envío",
                                value=current_date,
                                key=f"new_date_{idx}"
                            )
                            new_name = st.text_input(
                                "Nombre del revisor",
                                value=row["nombre_revisor"],
                                key=f"new_name_{idx}"
                            )
                            new_email = st.text_input(
                                "Email del revisor",
                                value=row["email_revisor"],
                                key=f"new_email_{idx}"
                            )
                            col_save, col_cancel = st.columns(2)
                            with col_save:
                                save = st.form_submit_button(
                                    "💾 Guardar", use_container_width=True
                                )
                            with col_cancel:
                                cancel = st.form_submit_button(
                                    "Cancelar", use_container_width=True
                                )

                            if save:
                                df_rev.loc[idx, "fecha_envio_revision"] = (
                                    datetime.combine(
                                        new_date, datetime.min.time()
                                    ).isoformat(timespec="seconds")
                                )
                                df_rev.loc[idx, "nombre_revisor"] = new_name.strip()
                                df_rev.loc[idx, "email_revisor"] = new_email.strip()
                                save_revisions(df_rev)
                                st.session_state[f"editing_{idx}"] = False
                                st.success("Actualizado.")
                                st.rerun()

                            if cancel:
                                st.session_state[f"editing_{idx}"] = False
                                st.rerun()

                    if st.button(
                        "🗑️ Eliminar asignación",
                        key=f"del_{idx}",
                        help="Eliminar este revisor del manuscrito"
                    ):
                        df_rev = df_rev.drop(index=idx).reset_index(drop=True)
                        save_revisions(df_rev)
                        st.success("Asignación eliminada.")
                        st.rerun()

    # ---------- TAB 4: FULL LOG ----------
    with tab4:
        st.subheader("Full logs")

        # --- Author confirmations ---
        st.markdown("#### ✅ Author confirmations")
        df_conf = read_confirmations()
        if df_conf.empty:
            st.info("No confirmations registered yet.")
        else:
            df_display = df_conf.drop(columns=["token"])
            st.dataframe(df_display, use_container_width=True)
            csv_data = df_conf.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ Download confirmations CSV",
                data=csv_data,
                file_name=f"confirmations_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv"
            )

        st.divider()

        # --- Peer review log ---
        st.markdown("#### 🔍 Revisiones (log)")
        df_rev = read_revisions()
        if df_rev.empty:
            st.info("Aún no hay revisores asignados.")
        else:
            df_rev_show = df_rev.copy()
            df_rev_show["dias_transcurridos"] = df_rev_show[
                "fecha_envio_revision"
            ].apply(days_since)
            df_rev_show["dias_transcurridos"] = df_rev_show[
                "dias_transcurridos"
            ].apply(format_days)

            st.dataframe(df_rev_show, use_container_width=True)

            csv_data_rev = df_rev.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ Download reviews CSV",
                data=csv_data_rev,
                file_name=f"reviews_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv"
            )

# ============================================================
# AUTHOR VIEW (public)
# ============================================================
def author_view(token: str):
    st.title("✍️ Manuscript Approval")
    data, error = validate_token(token)
    if error:
        st.error(error)
        return

    email = data["email"]
    article_id = data["articulo_id"]
    name = data["nombre"]

    df_art = read_articles()
    df_conf = read_confirmations()

    art_row = df_art[df_art["articulo_id"] == article_id]
    if art_row.empty:
        st.error("The associated article no longer exists.")
        return
    art = art_row.iloc[0]

    st.write(
        f"Dear **{name}**, you have been invited to approve "
        f"the following manuscript:"
    )
    st.markdown(f"### {art['titulo']}")
    st.caption(f"Editorial reference: {article_id}")

    if art["ruta_pdf"]:
        st.markdown(f"📄 [View manuscript PDF]({art['ruta_pdf']})")

    row = df_conf[(df_conf["articulo_id"] == article_id) &
                  (df_conf["email"] == email)]
    if row.empty:
        st.error("Your author record for this article was not found.")
        return

    already_approved = row.iloc[0]["aprobado"] == "1"

    if already_approved:
        st.success(
            f"✅ You have already approved this manuscript on "
            f"{row.iloc[0]['fecha_aprobacion']}."
        )
        st.info("Thank you for your confirmation.")
        return

    with st.form("approval_form"):
        st.write("Please confirm the following:")
        accept = st.checkbox(
            "I have read the manuscript in full and I agree with its "
            "content for publication."
        )
        comments = st.text_area(
            "Optional comments for the editor", height=120
        )
        submit = st.form_submit_button(
            "✅ Confirm approval", use_container_width=True
        )

    if submit:
        if not accept:
            st.warning("You must check the box to confirm your approval.")
            return

        idx = df_conf.index[
            (df_conf["articulo_id"] == article_id) & (df_conf["email"] == email)
        ]
        df_conf.loc[idx, "aprobado"] = "1"
        df_conf.loc[idx, "fecha_aprobacion"] = datetime.now().isoformat(
            timespec="seconds"
        )
        df_conf.loc[idx, "comentarios"] = comments
        save_confirmations(df_conf)

        st.success("Thank you! Your approval has been recorded.")
        st.balloons()

        article_authors = df_conf[df_conf["articulo_id"] == article_id]
        if (article_authors["aprobado"] == "1").all():
            df_art.loc[df_art["articulo_id"] == article_id, "estado"] = "todos_aprobados"
            save_articles(df_art)
            send_editor_notification(
                f"All authors of article {article_id} "
                f"('{art['titulo']}') have approved the manuscript. "
                f"You may proceed to publish it."
            )

# ============================================================
# MAIN ROUTER
# ============================================================
def main():
    # One-shot migration of revisiones.csv to the new 5-column layout.
    # Runs only when the editor is authenticated (so the public author
    # view stays lightweight and doesn't touch the reviews file).
    if "editor_authenticated" in st.session_state and st.session_state.editor_authenticated:
        migrate_revisions_csv()

    query_params = st.query_params
    token = query_params.get("token")

    if token:
        author_view(token)
    else:
        if authenticate_editor():
            # Migrate once after successful login
            migrate_revisions_csv()
            editor_view()

if __name__ == "__main__":
    main()
