# Security policy

MicMic listens to you, reads your screen when you ask, drives other apps and a browser,
and sends messages on your behalf. A security bug here can be serious, so please
report it privately.

## Reporting a vulnerability

Email **Zoharmosseri@gmail.com** with "MicMic security" in the subject. Please do not
open a public issue, discussion or pull request for a vulnerability.

Include what you can:

- what an attacker can do, and what they need first (a website you visit, a message
  you receive, a file on the Mac, local access);
- the steps to reproduce, and the MicMic version or commit;
- whether it affects the Mac app, the local server, or the MicMic cloud (`proxy/`).

You will get an answer within a few days. We will keep you posted while it is fixed,
and credit you in the release notes if you would like.

## What is in scope

- Anything that makes MicMic act without the user asking: prompt injection through a
  web page, a document, a message or the screen that leads to a send, a call, a
  purchase step, or a click the user did not request.
- Any way around the hard refusals: sending without a read-back, reaching a password,
  card or identity field, pressing a final purchase button, or deleting anything.
- The local server on `127.0.0.1`: requests from a web page or another local process
  that it should not accept.
- The MicMic cloud: device tokens, metering, the Stripe webhook, or reading another
  device's data.
- Data leaving the Mac that the [privacy section](README.md#privacy-in-plain-words) says
  does not.

## Supported versions

Security fixes go into the latest release. The app checks for updates and tells you
when a newer one is out.
