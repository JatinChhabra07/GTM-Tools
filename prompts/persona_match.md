# Match job titles to buyer personas

The trusted input lists our personas (name + example titles + seniority) and job titles that
didn't match by text. For each title, return the persona it belongs to, or null.
Match on function and seniority, not words: "Founder & Chief Executive" -> Founder/CEO;
"Growth Marketing Lead" can match "Head of Growth" only if the persona's seniority allows a
lead/manager. Interns, assistants, students, recruiters, freelancers, advisors and investors are
always null. When unsure, null.
