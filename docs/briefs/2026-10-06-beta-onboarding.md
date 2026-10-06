# Brief: CareAgents beta-tester onboarding (10 testers by Oct 31)

**Decision:** build a small `/beta` page and a CLI list. Reuse the rest.
Do not automate Sendblue. Sample records only.

**User problem:** someone who got the link from a physician advisor or saw
the Oct 20 LinkedIn post opens it on a phone. They need to know what this
is, try it in 15 minutes, and tell us what broke.

**Two caps.** The 25-invite cap (`STAGE1_INVITE_CAP`) is for *real-record*
testers and stays closed until #565. The 10-contact Sendblue sandbox limits
**iMessage only**. Nobody waits for the web app.

## 1. `/beta` page

A static explainer with one short form. It says: CareAgents is an assistant
that reads health records and drafts paperwork, and you approve anything it
sends. It is not a doctor, and it does not give medical advice. During the
beta you use made-up sample records, not your own.

Three steps: 1) sign up with your email, 2) pick the sample records and ask
a question, 3) try an approval. Under that: links to the demo videos and the
form (first name, email, optional mobile "for iMessage", and an unticked box
"You can contact me about the beta").

- GET `/beta` and `/beta?ref=x` return 200. At 375px there is no horizontal
  scroll and body text is at least 16px.
- No "tenant" or "FHIR". Step 2 says "sample" or "made-up".
- The form refuses to submit while the consent box is unticked.
- `ref` is stored as a plain string of at most 32 characters, matching
  `[a-z0-9-]`. Any other value is dropped, and the request is not refused.

## 2. After submit

The page shows "Thanks. Check your email." The owner gets one
`mail.send_notice` email: first name, email domain, whether a mobile was
given, and the ref. The full number is not in the email. The owner reads it
in the CLI.

The tester gets: "You're in. Open careagents.cloud and sign up with this
email." A second email, "Text hi to <number>", goes out only when the owner
marks the number `added`, so we never tell anyone to text a line that won't
answer (wording depends on Q1). Past 10: "iMessage is full for now. We'll
email you when a spot opens. The web app works today."

**Value gate on automating the Sendblue add: negative.** The docs list a
contacts API (`POST /api/v2/contacts`) but say a contact is verified when
the contact texts the line first. Nothing says an API-added contact is
verified or counts toward the 10. Ten dashboard clicks don't justify a build.

- A submit creates one `ca_beta_requests` row with status `new`. A second
  submit with the same email updates that row.
- The owner sees `iMessage: waitlist` for each request after the 10th
  request that gave a mobile.
- With no mail provider set up, the submit still succeeds.

## 3. First run

There is no new tour. `chat.html` already shows four starters, and the
first one (the intake form) ends in an approval. Add one thing: a hub tile,
"Text your assistant", shown only for sample accounts while Sendblue is on.
It says: "Texts go through an outside messaging service. Use it with
sample records only."

- A stranger goes from sign-up to a finished sample approval at 375px
  without help (patient-tester run).
- The iMessage tile is not shown for an account with a real connection.

## 4. Feedback

Every tester email and the hub footer link to `mailto:contactus@healthclaw.io`,
with the subject "tester" already filled in. The weekly check-in is a
Monday email the owner sends by hand to testers with status `active`.

- The mailto link opens with the subject already filled in, on iOS Mail
  and Gmail.

## 5. Privacy

We keep first name, email, optional mobile and ref. These are account data,
not PHI.

- Delete the mobile once its status is `added`, or after 30 days.
- Delete a request with no account after 60 days.
- Each tester email has a single-use "Remove my request" link that works
  without signing in.
- `delete_account` also deletes the person's `ca_beta_requests` row, with a
  test.

Page copy: "We keep your name, email and phone only to run the beta.
Remove them any time from the link in our email, or delete your account in
Settings."

## 6. Owner view

Add `flask beta-requests list`, next to `invites list`. It shows date,
first name, email, mobile (in full only while `new`), ref, and status (`new`, `added`,
`waitlist`, `active`, `removed`). Add `flask beta-requests mark <email>
added`. `weekly-counts` stays as it is.

## 7. Not building

- An admin web page
- A guided tour or onboarding checklist
- Scheduled check-in emails
- A referral leaderboard or referral rewards
- Real records over iMessage (blocked until Sendblue has a HIPAA instance
  and a BAA)
- Changes to the real-record invite flow

## Open questions for the founder

1. **Sendblue:** ask their support whether a contact added in the dashboard
   still has to text first, and whether the 10 includes your own phone. If
   texting first is enough, cut the mobile field and the owner email, and
   make `/beta` a static page with "Start" and "Text us".
2. Will the advisor's link carry a `ref`? What value?
3. Is 30/60 days the retention you want written into the privacy policy?
   That policy already has #565 open.
