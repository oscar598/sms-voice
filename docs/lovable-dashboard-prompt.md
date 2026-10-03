Build a single-page, read-only dashboard called "Referral Closure Dashboard" for an SMS referral agent pilot in Nairobi. Front-end only: no Supabase, no auth, no database.

DATA: Poll GET {API_BASE}/api/dashboard every 3 seconds with fetch(). Always send the header "ngrok-skip-browser-warning: 1". API_BASE is entered by the user in a small settings field at the top right (persist it in localStorage; default empty). If API_BASE is empty or the fetch fails, show a clear inline state "Can't reach the referral API. Check the tunnel URL." with the URL shown. Never invent numbers or show placeholder data.

Response shape (example, real field names):
{"generated_at":"2026-10-05T12:00:00","totals":{"cases":40,"completed":18,"completed_by_clinic":14,"completed_by_patient":4,"unresolved":21,"lost":1,"escalated_now":4},"completion_rate":0.45,"median_days_to_completion":2.7,"share_resolved_without_chw":0.9,"top_barriers":[{"barrier":"transport","cases":8},{"barrier":"clinic_closed","cases":5},{"barrier":"cost","cases":5}],"radar":{"window_days":7,"min_n":5,"barriers":["clinic_closed","turned_away"],"facilities":[{"facility_id":"FAC-B","name":"Demo Baraka Clinic","area":"Embakasi","flagged":6,"n":11,"share":0.545,"hidden":false},{"facility_id":"FAC-A","name":"Demo Mto Health Centre","area":"Kayole","flagged":1,"n":12,"share":0.083,"hidden":false}]},"unreachable":[],"cases":[{"id":"R-0023","status":"contacted","escalation":"none","latest_barrier":null,"facility":"Demo Mto Health Centre","visit_date":null,"completed_by":null,"referred_at":"2026-10-04T21:36:00"},{"id":"R-0031","status":"contacted","escalation":"none","latest_barrier":null,"facility":"Demo Upendo Eye Unit","visit_date":null,"completed_by":null,"referred_at":"2026-10-04T16:48:00"}]}

LAYOUT, top to bottom:
1. Header: title, a small "Synthetic demo data" badge, "Updated <generated_at>" timestamp, and the API_BASE field.
2. KPI row of 4 cards: Completion rate (completion_rate as %, subtext "<completed_by_clinic> clinic-confirmed / <completed_by_patient> patient-reported"); Unresolved (totals.unresolved, subtext "<escalated_now> with a health worker"); Median time to completion (median_days_to_completion days, or "-" if null); Resolved without a health worker (share_resolved_without_chw as %).
3. "Clinic Reliability Radar" (the hero section, give it the most space): one horizontal bar per radar.facilities item, already sorted by the API. Label each "<name> (<area>): <flagged> of <n> patients reported it closed or turned them away, last <window_days> days". Bar length = share. Highlight the top non-hidden facility in a warning color. Facilities with hidden=true are greyed out with the text "too few cases (n < <min_n>)". Under the section, small text: "Signal, needs follow-up. Based on patient reports."
4. "Why referrals stall": horizontal bar chart of top_barriers (barrier names humanized: clinic_closed -> Clinic closed, etc.), counts = number of cases.
5. "Unreachable patients": table of unreachable (case_id, phone, error_code, failed_at). Empty state: "Every patient was reachable."
6. "Cases": table of cases (id, facility, status, escalation, latest_barrier, visit_date). Status as colored chips: completed green, lost grey, escalated amber, others neutral.

STYLE: calm, clinical, high contrast, works in light and dark mode, responsive down to 375px width. No emoji.

---

FOLLOW-UP PROMPT (send after the dashboard is built):

Add a "New referral" button in the header that opens a dialog with a form:
- Patient phone (text, e.g. 0712 345 678)
- Facility (select, options from GET {API_BASE}/api/facilities: [{id, name, area, services, hours_text}], label "<name> (<area>)")
- Service (select, options = the chosen facility's services)
- Patient's area (select of the distinct facility areas, defaults to the chosen facility's area)
- Referral token (password field in the settings area next to API_BASE, stored in localStorage)

On submit, POST {API_BASE}/api/referrals with JSON {patient_phone, facility_id, service, area} and headers "Content-Type: application/json", "Authorization: Bearer <token>", "ngrok-skip-browser-warning: 1".
- 201 -> {case: {id, ...}, intro_sent: bool}: close the dialog and toast "Created <id>. The patient has been texted." (or, if intro_sent is false, "Created <id>, but the first SMS failed. See Unreachable patients.").
- 400 or 409 -> {errors: {field: message}}: show each message under its field.
- 401 -> "Wrong or missing referral token." 503 -> "Referrals are turned off on the server."
