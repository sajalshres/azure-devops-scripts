def send_email_with_csv_if_updates(recipients, csv_file):
    """
    Send email with CSV attachment only if CSV exists and has updates.
    Each recipient is sent separately to avoid SMTP disconnect issues.
    Includes retry and graceful fallback handling.
    """
    import smtplib
    from email.message import EmailMessage
    import os
    import time

    SMTP_SERVER = "appmailrelay.fcpd.fcbint.net"
    SMTP_PORT = 25
    DEFAULT_DEVSECOPS_EMAIL = "devsecops@firstcitizens.com"

    # Ensure file exists
    if not os.path.exists(csv_file):
        print(f"[INFO] CSV file '{csv_file}' not found. Skipping email.")
        return

    # Skip if empty (no updates)
    if os.path.getsize(csv_file) == 0:
        print("[INFO] CSV file is empty. No updates to report. Skipping email.")
        return

    recipients = recipients or [DEFAULT_DEVSECOPS_EMAIL]

    for recipient in recipients:
        try:
            msg = EmailMessage()
            msg["From"] = "devops@firstcitizens.com"
            msg["To"] = recipient
            msg["Subject"] = "Azure DevOps Release Approval Audit - Summary Report"
            msg.set_content(
                "Hello Team,\n\n"
                "Please find attached the latest Azure DevOps release approval audit report.\n\n"
                "Regards,\nDevSecOps Automation"
            )

            with open(csv_file, "rb") as f:
                msg.add_attachment(
                    f.read(),
                    maintype="application",
                    subtype="octet-stream",
                    filename=os.path.basename(csv_file),
                )

            # Retry up to 3 times for each recipient
            for attempt in range(3):
                try:
                    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
                        server.ehlo()  # Proper handshake
                        server.send_message(msg)
                    print(f"[INFO] Email sent to: {recipient}")
                    break  # success
                except smtplib.SMTPServerDisconnected:
                    print(f"[WARN] Disconnected while sending to {recipient}, retrying ({attempt+1}/3)...")
                    time.sleep(2)
                except Exception as e:
                    raise e
            else:
                print(f"[ERROR] Giving up after 3 attempts for {recipient}")

        except Exception as e:
            print(f"[ERROR] Failed to send email to {recipient}: {e}")
