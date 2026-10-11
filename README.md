# IEEE Research Paper Citation Automation System

A fully automated n8n workflow that formats your research paper for IEEE journal submission — **without ever changing your original text**.

---

## What This System Does

1. Accepts your Word paper (`.docx`) and an IEEE journal template
2. Backs up your original document (untouched)
3. Extracts text and existing citation markers
4. Verifies each reference against Crossref, DOI.org, and your Zotero library
5. Formats all citations and bibliography to IEEE style
6. Applies the journal template layout
7. Compares original vs final text to prove nothing was changed
8. Generates detailed reports

**Your original text is NEVER modified.**

---

## Folder Structure

```
d:\bot\
├── README.md                    <- This file
├── setup.bat                    <- One-click Windows installer
├── start_server.bat             <- Start the upload/download web server
├── n8n_workflow.json            <- Import this into n8n
├── requirements.txt             <- Python dependencies
├── .env.example                 <- Copy to .env and fill in your keys
|
├── scripts\
|   ├── extract_document.py      <- Stage A: Read & inspect Word doc
|   ├── verify_references.py     <- Stage B: Check references online
|   ├── match_citations.py       <- Stage C: Map citations to text
|   ├── format_citations.py      <- Stage D & E: IEEE formatting
|   ├── apply_template.py        <- Stage F: Apply journal template
|   ├── validate_document.py     <- Stage G: Compare original vs final
|   ├── zotero_client.py         <- Zotero Web API connector
|   ├── crossref_client.py       <- Crossref & DOI lookup
|   └── report_generator.py      <- Generate all output reports
|
├── server\
|   └── app.py                   <- Flask upload/download web server
|
├── uploads\                     <- Your uploaded files go here
├── outputs\                     <- Processed files are saved here
└── logs\                        <- Processing logs
```

---

## Quick Start (Step-by-Step for Beginners)

### Step 1: Install Python

1. Go to https://www.python.org/downloads/
2. Download Python 3.11 or newer
3. Run the installer - CHECK "Add Python to PATH"
4. Open Command Prompt and type `python --version` to confirm

### Step 2: Install n8n Desktop

1. Go to https://n8n.io/
2. Click Download -> select Windows
3. Install and open n8n
4. n8n will open in your browser at http://localhost:5678

### Step 3: Get Your Zotero API Key

1. Go to https://www.zotero.org/settings/keys
2. Click "Create new private key"
3. Name it: n8n-ieee-workflow
4. Check: Allow library access (read only is fine)
5. Copy the key - you will need it in Step 4
6. Also note your Zotero User ID (shown at the top of that page)

### Step 4: Configure the System

1. In the d:\bot\ folder, copy .env.example to .env
2. Open .env in Notepad and fill in:
   ZOTERO_API_KEY=your_key_here
   ZOTERO_USER_ID=your_user_id_here
   ZOTERO_LIBRARY_TYPE=user
   CROSSREF_EMAIL=your_email@example.com

### Step 5: Install Python Libraries

Double-click setup.bat OR run in Command Prompt:
   cd d:\bot
   pip install -r requirements.txt

### Step 6: Import the n8n Workflow

1. Open n8n at http://localhost:5678
2. Click + (New Workflow)
3. Click the menu -> Import from File
4. Select d:\bot\n8n_workflow.json
5. Click Save

### Step 7: Start the Web Server

Double-click start_server.bat
A browser will open at http://localhost:8080

### Step 8: Run Your Workflow

1. Open http://localhost:8080
2. Upload your research paper (.docx)
3. Upload your IEEE template (.docx) if you have one
4. Click Run Workflow
5. Wait 2-5 minutes
6. Download your formatted paper and reports

---

## Output Files

| File | Description |
|------|-------------|
| IEEE_Formatted_Research_Paper.docx | Your paper with IEEE citations |
| Original_Research_Paper_Backup.docx | Untouched copy of your original |
| Citation_Verification_Report.csv | Every citation checked |
| Text_Preservation_Report.txt | Proof original text was unchanged |
| Unresolved_Citations_Report.txt | Citations needing your review |

---

## Troubleshooting

- "Python not found" -> Re-install Python and check "Add to PATH"
- "Port 8080 in use" -> Change PORT=8080 to PORT=8081 in .env
- "Zotero connection failed" -> Check your API key and User ID in .env
- "No references found" -> Make sure paper has [1], [2] style citations
