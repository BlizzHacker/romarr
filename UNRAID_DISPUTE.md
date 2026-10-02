# Unraid Community Applications Dispute Report

## Filing
**Date:** October 2, 2026
**Filed by:** Wade (BlizzHacker) — me@moveweight.com
**Repository:** github.com/BlizzHacker/cartridge-unraid
**Disputed submission:** "ROMarrNG" — submitted by user "snapetech"
**Unraid CA listing (contested):** https://ca.unraid.net/apps/romarrng-0u7zgr30thxjr7

---

## 1. What happened

In August 2026, a user named "snapetech" submitted a fork of our ROMarr project
to the Unraid Community Applications repository under the name "ROMarrNG".
That submission:

- Copied the entire ROMarr codebase from github.com/BlizzHacker/romarr without
  authorization or attribution
- Retained the original ROMarr branding, architecture, and Unraid CA template
  structure
- Rebranded it with a slightly different name ("ROMarrNG") to appear as an
  independent product
- Was submitted to Unraid CA after our original submission was taken down,
  making it appear that "ROMarrNG" is a legitimate alternative

This is not a fork in the open-source sense. Our repository is MIT-licensed,
but the fork removes our branding, removes our name from the CA template
description, and presents itself as a separate project. That is not how MIT
license works — the license requires attribution and preservation of the
copyright notice in all substantial portions of the software.

## 2. Evidence

### 2a. Code similarity

Our v0.9.0 release (2026-09-19) and the "ROMarrNG" submission both contain
identical:

- Store architecture (queue item tracking, seerr request tracking)
- API route structure (/api/v1/*)
- Unraid CA template structure (romarr.xml with identical field names)
- Docker entrypoint flow
- Platform detection logic
- Import pipeline (decompress → catalogue → import)

We can provide a line-by-line diff on request.

### 2b. Branding theft

The "ROMarrNG" Unraid CA page (when it was live) used our:

- Project name in the description
- Feature list (verbatim)
- Platform list (verbatim)
- Configuration guide structure
- "© MoveWeight Studios" reference removed, replaced with nothing

### 2c. Submission timeline

| Date | Event |
|------|-------|
| 2025-12 | We submit ROMarr v0.7.x to Unraid CA |
| 2026-08-08 | Our repo (cartridge-unraid) becomes inaccessible from GitHub — Unraid CA page for our repo was removed |
| 2026-08-09 | "ROMarrNG" appears in Unraid CA under snapetech |
| 2026-09-19 | We cut v0.9.0, restored our repo visibility |
| 2026-09-21 | PR on main repo fails CI (expected — code was in flux) |
| 2026-10-02 | We cut v1.0.0 with integration API (our genuine differentiator) |

The timing is significant: our submission went down the day before their
submission went up.

## 3. Our request

We request that Unraid Community Applications:

1. **Remove the "ROMarrNG" submission** from the CA repository, as it is an
   unlicensed (in the attribution sense) and unauthorized fork
2. **Restore our original ROMarr submission** (or accept our v1.0.0 resubmission),
   which we are prepared to submit immediately
3. **Flag the "snapetech" account** for review — this appears to be a
   deliberate brand-hijacking submission, not a good-faith fork

## 4. Our resubmission

We are resubmitting ROMarr at v1.0.0, which includes:

- All features that were in the prior submission
- New: External Platform API (Cartridge + custom frontend integration)
- New: Request tracking with stable request IDs across restarts
- New: Startup recovery of in-flight external requests
- 2083 passing tests (v0.9.0 had ~2081)
- Full OpenAPI documentation for all API routes
- NOTICE in the repository root establishing authorship

Our repository is public: https://github.com/BlizzHacker/romarr
Our CA template repository is public: https://github.com/BlizzHacker/cartridge-unraid

## 5. Contact

Wade (BlizzHacker)
me@moveweight.com
GitHub: github.com/BlizzHacker

---

*This report is filed in good faith. We are happy to provide further evidence,
a full code diff, or to meet with Unraid staff to resolve this.*
