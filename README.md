<h1>OrientAR - METU NCC Events Web Scraper</h1>

<h2>Project Overview</h2>

<p>
This module is responsible for automatically collecting campus event information from the official 
METU Northern Cyprus Campus website and storing it in the OrientAR database.
</p>

<p>
The scraper specifically targets the 
<b>"This Week on Campus"</b> page and extracts structured event information such as 
event dates, times, titles, descriptions, and locations.
</p>

<p>
The collected data is stored in <b>Firebase Firestore</b> and later used by the OrientAR 
mobile application and AI-powered chatbot to provide students with up-to-date campus event information.
</p>

<hr>

<h2>Main Features</h2>

<ul>
<li>Automatic scraping of the <b>This Week on Campus</b> page</li>
<li>Extraction of event titles, dates, times, and descriptions</li>
<li>Automatic detection of event locations</li>
<li>Duplicate detection using SHA256 hashing</li>
<li>Storage of structured data in <b>Firebase Firestore</b></li>
<li>Automatic weekly execution using <b>GitHub Actions</b></li>
<li>Society information scraping from METU NCC societies page</li>
</ul>

<hr>

<h2>Build Scripts</h2>

<p>
To run the scraper locally, make sure the following tools are installed:
</p>

<ul>
<li>Python 3.11 or later</li>
<li>pip (Python package manager)</li>
<li>Internet connection</li>
<li>Firebase Service Account credentials</li>
</ul>

<h3>Required Environment Variable</h3>

<p>
For security reasons, Firebase credentials are not stored in the repository. 
Developers must configure the following environment variable before running the scraper:
</p>

<ul>
<li><b>FIREBASE_SA_B64</b> – Base64 encoded Firebase Service Account JSON file</li>
</ul>

<h3>Install Dependencies</h3>

<pre>
pip install -r requirements.txt
</pre>

<h3>Run the Scraper</h3>

<pre>
python scraper.py
</pre>

<hr>

<h2>Technologies Used</h2>

<ul>
<li>Python</li>
<li>Requests (HTTP requests)</li>
<li>BeautifulSoup (HTML parsing)</li>
<li>lxml parser</li>
<li>Firebase Firestore</li>
<li>GitHub Actions (automation)</li>
</ul>

<hr>

<h2>Automation</h2>

<p>
The scraper runs automatically every week using <b>GitHub Actions</b>.
</p>

<ul>
<li>Execution Time: <b>Every Monday at 21:00 (Turkey Time)</b></li>
<li>Scheduler: GitHub Actions CRON</li>
<li>Workflow: Scrapes event data and updates Firestore if changes are detected</li>
</ul>

<p>
The system calculates a SHA256 hash of the scraped data. If the content has not changed, 
the database is not updated, preventing unnecessary writes.
</p>

<hr>

<h2>Data Flow</h2>

<ol>
<li>Scraper fetches HTML from METU NCC website</li>
<li>HTML is parsed using BeautifulSoup</li>
<li>Event blocks are detected and structured</li>
<li>Data is cleaned and normalized</li>
<li>SHA256 hash comparison checks for updates</li>
<li>Firestore database is updated if new content exists</li>
</ol>

<hr>

<h2>Source Pages</h2>

<ul>
<li>This Week on Campus: https://ncc.metu.edu.tr/this-week-on-campus</li>
<li>Student Societies: https://ncc.metu.edu.tr/socialandculturalaffairs/societies-communication-details</li>
</ul>

<p>
This data is used by the <b>OrientAR mobile application</b> and the <b>OrientAR AI chatbot</b> 
to provide real-time campus information to students.
</p>
