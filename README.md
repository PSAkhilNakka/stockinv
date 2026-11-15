# Hardware Desk

A working, database-backed hardware store management application for one Indian store branch. The data model includes branch keys for later expansion. The UI is desktop first and supports mouse-driven POS billing.

## What is implemented

- Admin, manager, cashier roles; password login, session CSRF protection, audit log, and first-admin provisioning.
- Products, categories, brands, custom units and product-specific conversions, opening stock, adjustments, movement history, price history, CSV/XLSX import, Excel export, low-stock reorder suggestions.
- Suppliers, purchase orders, purchase drafts and confirmation, goods receipts, supplier payments and allocations, returns and debit notes.
- Mouse-driven POS with category cards, quantity/rate/discount editing, held bills, a required payment-type choice before invoice generation, split payments, cash/UPI/card/bank/credit records, GST preview, invoice printing and PDF download, sales returns, credit notes and authorized cancellation.
- Customers, receivables, partial and independent payments, statements, quotations with revision and conversion, expenses, dashboard, inventory/profit/GST reports, Excel and PDF exports, GSTR-1 and GSTR-3B preparation worksheets.
- A4 invoice PDF, printable A4 and 80 mm receipts, delivery challan against a confirmed sale, document attachments on local disk or optional S3, PostgreSQL backup helper.

All posted stock changes create an immutable movement. A sale line freezes its rate, GST and weighted average cost. Purchase and sale posting, payments and stock changes commit as one database transaction. Held bills, quotations and purchase orders do not change inventory.

## Setup with Docker Compose and PostgreSQL

1. Install Docker Desktop. In this directory, create `.env` with a strong alphanumeric `POSTGRES_PASSWORD`, a long random `SECRET_KEY`, and optionally `S3_BUCKET`. Do not use the example secrets. Generate a key with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. Start the database and web app: `docker compose up --build -d`.
3. Create the first administrator: `docker compose exec web python manage.py init-admin --generate-password`. The command prints the **one-time username and password**; store them securely. There is no shared default credential.
4. Open `http://localhost:8000`, sign in, and set the store name, address, state and state code, GSTIN and GST registration status in Settings. Add products and suppliers before billing.

For a public deployment, put the web service behind a TLS reverse proxy, restrict database access, use a persistent PostgreSQL volume and a secrets manager, and set a suitable `S3_BUCKET` plus IAM policy if attachments should be stored in S3. The included Compose file is a local deployment baseline.

## Local Python setup

Use Python 3.12 or newer. Install the packages in `requirements.txt`. Set `DATABASE_URL` to a PostgreSQL URL such as `postgresql+psycopg://hardware:<password>@localhost:5432/hardware` and set `SECRET_KEY` in the shell environment. Run `python manage.py init-admin --generate-password`, then `python app.py`. `APP_HOST` and `APP_PORT` control the local bind address and port. If `DATABASE_URL` is omitted, the app uses local SQLite for development and tests only.

On Windows PowerShell, environment variables can be set for the current terminal with `$env:DATABASE_URL='postgresql+psycopg://...'` and `$env:SECRET_KEY='...'`. The `.env.example` file documents settings but is not loaded automatically by local Python runs.

## Business setup and accounting rules

- Set each product's base unit and its purchase and selling price **per base unit**. For example, a product priced at ₹2 per piece with `1 box = 50 pieces` defaults to ₹100 per box at checkout. Custom line rates are manager/admin actions.
- A confirmed purchase increases stock at the frozen, discounted taxable cost; recoverable input GST is excluded from inventory cost. A sale records weighted average cost at posting. Changing a product's current purchase price never rewrites old documents or sale profit.
- Returns post separate credit/debit documents, reverse the corresponding stock and ledgers, and retain the original invoice number. An invoice number is never reused.
- A quotation must be approved by a manager or administrator before conversion. The approved lines and discount percentages are preserved, and the final invoice is posted once.
- A credit sale requires a registered customer; the customer's ledger is the source of total receivables. Independent payments allocate to open invoices where possible. Expenses reduce net profit and inventory purchases are not counted again as operating expenses.
- GST is computed from the configured, effective-dated product or HSN rate and explicit place of supply. Tax exclusive and inclusive pricing are supported. Verify each HSN and rate with a qualified tax adviser before live use.

## Reports and exports

Dashboard and reports query posted documents. Dashboard filters accept `start` and `end` dates. Reports include daily/weekly/monthly/yearly, product/category/customer sales; supplier purchases; historical purchase price trends; inventory movements and 90-day stock ageing; profit, GST, expenses and customer/supplier outstanding. Purchase return price variance is shown separately from sales gross profit and included in net profit. `gstr1_prep` and `gstr3b_prep` are preparation worksheets for accountant review; the app does **not** file returns or determine eligible input tax credit.

## Backups and restore

Back up PostgreSQL daily with a matching `pg_dump` version and copy the encrypted backup to separate storage. `backup.py` runs `pg_dump` using `DATABASE_URL` and writes a timestamped custom-format dump to `BACKUP_DIR` (default `./backups`). Attachments in the `uploads` volume or S3 bucket need a separate backup/retention policy.

To restore, stop the web app, create a fresh database, and run `pg_restore --no-owner --no-acl --dbname=<target database URL> <backup.dump>`. Test this procedure in a nonproduction environment on a schedule before relying on it. A restore replaces the target database state, so take a safety copy first.

## Tests

Run `python -m pytest -q tests`. The integration suite covers the 14 acceptance workflow areas against isolated SQLite databases, including posting invariants, GST splits, returns, credit payments, held bills, quote conversion, exports and cashier restrictions. A browser check also completed the mouse-only POS hold/resume/payment flow and generated an invoice. PostgreSQL concurrency and deployment checks require a running PostgreSQL instance; they were not available in the authoring environment.

## Live-use limits

The application records payment methods; it does not charge cards or initiate UPI/bank transfers. WhatsApp sharing uses a share link and depends on the user's WhatsApp client. For businesses subject to Indian e-invoicing, B2B GST tax invoices are blocked when `e_invoice_applicable` is enabled because this build has no IRP integration, IRN or signed QR workflow. GST return worksheets are for preparation, not filing. Validate the legal invoice format, current tax notifications and operational controls for your store before using the app for statutory billing.

Relevant official references: [CBIC invoice and credit note rules](https://cbic-gst.gov.in/pdf/amended-01012022-CGST-Rules-2017-Part-A.pdf), [IGST place of supply rules](https://cbic-gst.gov.in/hindi/IGST-bill-e.html), [GST Council rate reform FAQ](https://gstcouncil.gov.in/sites/default/files/2025-09/faq.pdf), [GSTN e-invoice overview](https://tutorial.gst.gov.in/downloads/news/e_invoice_overview.pdf).
