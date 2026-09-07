# MagnetoClip Privacy Policy

**Effective date:** September 4, 2026

This Privacy Policy explains how Devallix ("**Devallix**", "**we**", "**us**",
or "**our**") handles information when you use the MagnetoClip desktop
application, its companion browser extension, remote-dashboard access, and the
MagnetoClip website (together, the "**Service**").

**The short version:** MagnetoClip is a local-first application. Your downloads,
download history, and analytics stay on your own computer. We do not sell your
data, we do not run advertising, and we do not collect or view the content you
download. The only things we process are what is required to bind your license
to your computer and to operate the Service.

---

## 1. Information We Collect

We intentionally collect the minimum necessary to run the Service:

**1.1. License activation data.** To activate a serial key we process a
device identifier generated on your computer (a "machine ID"), a short
hostname describing the machine, the serial key itself, and the app version.
This is what allows a key to be bound to one computer and recognized on later
starts. See [Section 5](#5-how-we-use-information).

**1.2. Network activity required by the features you use.** Downloading a file
requires contacting the server that hosts it; downloading a torrent requires
contacting trackers and peers; checking for updates requires contacting the
update server. This activity is inherent to the features you choose to use and
is governed by those servers' own policies.

**1.3. Error and support information.** If you contact support, you may
voluntarily provide a diagnostics report (see [Section 6](#6-where-information-is-stored)).
You may also email us directly; any information you include in that email is
used only to help you.

**1.4. Website usage.** The MagnetoClip website may use essential functionality
such as web-server logs (IP address, browser type, pages requested) for secure
and reliable operation. We do not use advertising trackers.

## 2. What We Do NOT Collect

We do **not** collect, store, view, or transmit:

- the files you download, or the URLs of downloaded content;
- your download history, category statistics, or analytics;
- the contents of pages you visit, or the files detected on them;
- your browsing history, bookmarks, or other browser data;
- your proxy credentials (these are stored only in your operating system's
  credential store and are never transmitted to us).

## 3. Browser Extension

The MagnetoClip companion extension runs inside your browser and communicates
only with your local copy of MagnetoClip on the same computer through a
native-messaging channel.

**3.1.** When you visit a page, the extension may inspect page content to
detect downloadable files and media streams, and to fetch browser-local
resources (such as `blob:` URLs). This analysis happens on your computer and
the results are delivered to your local MagnetoClip process.

**3.2.** None of this information is sent to Devallix or any other server
except where the download itself requires contacting the third-party source you
chose.

**3.3.** You control what the extension does: toggling browser integration,
choosing whether to capture media streams, deciding whether MagnetoClip
becomes your default downloader, and confirming each capture — all from the
**Browser** page in MagnetoClip.

## 4. Remote Dashboard

The remote dashboard lets you view and control downloads from another device on
your **local network**. Access is protected by a pairing token that you
generate in MagnetoClip.

**4.1.** The dashboard communicates only over your local network and the
pairing token is required to connect.

**4.2.** The dashboard can pause, resume, and watch downloads; it never exposes
your filesystem and does not transmit data to us.

**4.3.** Regenerating the token invalidates all previously paired devices. For
example, use this when you no longer want a device to have access.

## 5. How We Use Information

We use the limited information we process only for these purposes:

- **To activate and validate licenses** (device binding, license status);
- **To provide the features you choose to use** (downloads, torrents, media
  capture, updates, remote dashboard);
- **To operate and secure the Service** (preventing abuse, resolving technical
  issues);
- **To communicate with you** when you contact support.

We do not use your information for advertising, profiling, or unrelated
research.

## 6. Where Information Is Stored

The vast majority of your data never leaves your machine. It is stored:

| Data | Location |
|---|---|
| Downloads database (SQLite) | Per-user Microsoft AppData (application data) directory |
| Application settings | Per-user configuration directory |
| Your serial key | Windows Credential Manager (OS credential store) |
| Proxy passwords | Windows Credential Manager (never in the database) |
| Logs | Structured JSON logs in the log directory |

**6.1.** The Analytics dashboard is computed **locally** from your own download
history. Nothing is uploaded.

**6.2.** When you send a diagnostics report during a support conversation,
log credentials (such as proxy or account tokens) are scrubbed automatically
before the report leaves your machine, to the extent technically possible.

## 7. Cookies and Tracking

**7.1.** The MagnetoClip website does not use advertising or cross-site
tracking cookies. Any cookies or similar storage used on the website are
limited to essential functions (for example, security or load balancing).

**7.2.** The desktop application and browser extension do not use cookies and
do not perform user tracking.

## 8. Data Sharing and Disclosure

We do not sell, rent, or share your personal information with third parties for
their own marketing purposes. We may disclose limited information only:

- **With service providers** who help operate the Service under confidentiality
  obligations (for example, the server that hosts license validation);
- **To comply with the law** — when required by a lawful request (for example,
  a subpoena, court order, or legal process), or where we believe in good faith
  that disclosure is needed to protect the rights, property, or safety of
  Devallix, our users, or others;
- **In the event of a transfer** — in connection with a merger, acquisition, or
  sale of assets, in which case the policy covering your data continues to
  apply.

We have no obligation to retain any of your data, and we will not voluntarily
disclose our users' download activity.

## 9. Data Security

We take reasonable technical and organizational measures to protect the limited
information we process, including secure communication with the license server
and the use of the operating system's credential store for sensitive values.
Because a portion of your data (your downloads and history) is stored only on
your own device, its security depends partly on you: please keep your Windows
account secure and enable disk encryption such as BitLocker if you handle
sensitive files.

## 10. Data Retention and Deletion

**10.1.** **License records.** A license's binding record is retained while a
key is active and removed or anonymized when you deactivate the key and the
license lapses.

**10.2.** **Your local data.** Your downloads database, settings, and logs are
removed when you uninstall MagnetoClip and delete the application-data and
configuration directories. Instructions appear in the MagnetoClip User Guide.

**10.3.** **Deactivation.** To unlink a computer from a serial key, use
**Settings → License → Deactivate this PC…** in MagnetoClip.

## 11. Your Rights and Choices

Depending on where you live, you may have the right to access, correct, export,
or delete personal information relating to you, and to object to or restrict
certain processing. Because we hold almost no personal information about you
beyond license activation records, exercising these rights is typically as
simple as:

- **deactivating** your license on a device you no longer use; and
- **deleting** your local MagnetoClip data directories.

To make a request, or to ask a question about your data, contact us using the
details in [Section 14](#14-contact). We will respond within a reasonable
timeframe and, where applicable, within the period required by law.

## 12. Children's Privacy

The Service is not directed to children under the age of 13, and we do not
knowingly collect personal information from children. If you believe a child
has provided us personal information, contact us and we will take steps to
delete it.

## 13. Changes to This Policy

We may update this Privacy Policy from time to time. When we do, we will
revise the "Effective date" at the top of this page and, if the changes are
material, prominent notice will be provided on the MagnetoClip website or
within the application. Your continued use of the Service after changes take
effect constitutes acceptance of the updated policy.

## 14. Contact

**14.1.** If you have questions about this Privacy Policy or about how your
information is handled, please contact Devallix through the support channels
provided with the Software or on the MagnetoClip website. Please provide
enough detail so we can locate your record (for example, the email or serial
key associated with your license) — but do not include your serial key in
unsecured email.

---

*This Privacy Policy works together with the MagnetoClip
[Terms of Service](TERMS_OF_SERVICE.md) and
[End User License Agreement](EULA.md).*