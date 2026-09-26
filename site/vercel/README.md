# micmic.vercel.app

The public address of the MicMic site. Vercel serves nothing itself: every path is
rewritten to the MicMic cloud on Railway (`proxy/`), which renders the landing page,
pricing, privacy and the Stripe return pages. One source of truth, a nicer URL.

The app keeps talking to the Railway URL directly (`savta/cloud_url.txt`), so device
sign-ups see the caller's real address and are not all counted as Vercel's.

Deploy: `cd site/vercel && vercel deploy --prod`
