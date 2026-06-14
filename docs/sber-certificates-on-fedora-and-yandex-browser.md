# Sber certificates on Fedora and Yandex Browser

> Руководство описывает, как установить CA-сертификаты из банковских `.p7b`-бандлов
> на **Fedora**, обновить системное хранилище доверия, а затем импортировать клиентский
> `.p12` в **Yandex Browser**, когда сервис требует персональный сертификат.
> Установка `.p12`-сертификата необходима для доступа к Пульс, SberChat, SberJazz и
> другим сервисам Банка.

**Keywords:** authentication / аутентификация, CA certificate / сертификат УЦ,
certificate / сертификат, client certificate / клиентский сертификат, import / импорт,
installation / установка, private key / закрытый ключ, system trust / системное доверие,
trust store / хранилище доверия.

---

## Files from the Bank

| File | Use on Linux |
|------|--------------|
| `russiantrustedca.p7b` | Russian Trusted Root CA material (PKCS#7). Install into system trust so TLS chains validate. |
| `sberca-chain.p7b` | Sber CA chain (PKCS#7). Same: trust for HTTPS. |
| `*.p12` (example: `21670569.p12`) | Client certificate and private key for certificate-based login (mutual TLS). |

Do the **CA steps first**, then the **`.p12`** step when you need to identify yourself to the site with a certificate.

---

## Step 1 — Point the shell at your certificate folder

Set `CERTDIR` to the directory that contains the `.p7b` files (change the path if yours is different):

```bash
CERTDIR="$HOME/Documents/security/certs"
```

---

## Step 2 — Convert `russiantrustedca.p7b` to PEM

PKCS#7 may be DER or PEM. The first command tries DER; if it fails, the second tries PEM:

```bash
openssl pkcs7 -inform DER -print_certs -in "$CERTDIR/russiantrustedca.p7b" -out /tmp/russiantrustedca.pem \
  || openssl pkcs7 -inform PEM -print_certs -in "$CERTDIR/russiantrustedca.p7b" -out /tmp/russiantrustedca.pem
```

---

## Step 3 — Convert `sberca-chain.p7b` to PEM

```bash
openssl pkcs7 -inform DER -print_certs -in "$CERTDIR/sberca-chain.p7b" -out /tmp/sberca-chain.pem \
  || openssl pkcs7 -inform PEM -print_certs -in "$CERTDIR/sberca-chain.p7b" -out /tmp/sberca-chain.pem
```

---

## Step 4 — Confirm the PEM files contain certificates

```bash
grep -c "BEGIN CERTIFICATE" /tmp/russiantrustedca.pem /tmp/sberca-chain.pem
```

Each path should report at least `1`. If OpenSSL printed an error or the count is `0`, fix the `-inform` choice or the source file before continuing.

---

## Step 5 — Install anchors and refresh Fedora trust

Requires `openssl` (already used) and `sudo`:

```bash
sudo cp /tmp/russiantrustedca.pem /etc/pki/ca-trust/source/anchors/russiantrustedca.pem
sudo cp /tmp/sberca-chain.pem /etc/pki/ca-trust/source/anchors/sberca-chain.pem
sudo update-ca-trust
```

---

## Step 6 — Remove temporary PEM files (optional)

```bash
rm -f /tmp/russiantrustedca.pem /tmp/sberca-chain.pem
```

---

## Step 7 — Restart Yandex Browser

Close **all** Yandex Browser windows, start the browser again, and open the site you use (for example `https://hr.sberbank.ru/platform/dashboard`). The page should load over HTTPS using the updated trust store.

---

## Step 8 — (Optional) Check Certificate Manager

1. Open **Certificate Manager** (browser settings, or try `browser://certificate-manager` / `chrome://certificate-manager` if your build allows it).
2. Under **Local certificates**, turn on **Use imported local certificates from your operating system** if you see it.
3. Open **View imported certificates from Linux**. You may see Sber-related entries (for example **SberCA Root Ext** and **SberCA Ext**) under **Intermediate certificates**. Labels and grouping can vary; empty **Trusted** there does not by itself mean the install failed if the site already loads.

**Installed by you** can stay empty when you rely only on Fedora's anchors from Step 5.

---

## Step 9 — Import the client `.p12` in Yandex Browser

When the service asks for a **client certificate** (or lists one at login):

1. Open **Certificate Manager**.
2. Open **Your certificates** in the sidebar.
3. Open **View imported certificates from Linux**, then **Client certificates from platform** (wording may vary).
4. Click **Import**, select your `.p12` file, enter the bundle **password**.

After import, the certificate should appear in the list. The site may prompt you to **choose** that certificate when you sign in.

If you already keep the same client certificate in a system NSS store that Yandex reads, you might see it under Linux without a separate import; otherwise the steps above add it through the browser.

---

## Step 10 — (Optional) Trust CAs only inside Yandex

If you must not change Fedora system trust:

1. Build PEM files from the `.p7b` files as in Steps 2–3.
2. If the import dialog rejects a multi-certificate PEM file, split it into separate `.crt` files (one `BEGIN CERTIFICATE` … `END CERTIFICATE` block per file).
3. In **Certificate Manager**, go to **Local certificates** → **Installed by you** → **Trusted certificates** → **Import** and add the roots your policy allows.

For one machine and many apps, **Steps 5–7** are usually simpler.

---

## Quick verification

- [ ] Step 4 showed at least one certificate per extracted file.
- [ ] Step 5 finished without errors.
- [ ] After Step 7, the target HTTPS site opens as expected.
- [ ] After Step 9, client login works if the service requires a `.p12`.

---

## If something goes wrong

| Situation | What to do |
|-----------|------------|
| `openssl` fails on a `.p7b` | Run the other `-inform` (DER vs PEM); confirm the file is intact. |
| Site still does not load after `update-ca-trust` | Fully quit the browser; try again; rule out VPN, proxy, or local HTTPS scanning that replaces certificates. |
| HTTPS works but login does not | Complete Step 9; confirm the `.p12` password; watch for a certificate picker on the site. |
| New computer | Repeat from Step 1 on that Fedora install with the same bank files. |

---

## Security

Install CA material only from **sources you trust** (for example official bank instructions). New trusted CAs affect TLS validation for the whole system (when using Step 5). Store `.p12` files like private keys: strong password, restrictive file permissions, no sharing.

---

## Browser shortcuts

* `browser://certificate-manager`
* `chrome://settings/certificates`

If internal URLs are blocked, use **Settings** → **Privacy and security** / **Security** → certificate or HTTPS-related entries (names vary by version and language).
