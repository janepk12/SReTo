"""
sreto.analysis — the DSP pipeline, ported so a `pip install`d SReTo can
actually compute waterfalls, the IQ dashboard, the band correlator and the
soil-moisture retrieval without a separately-cloned copy of the sdr_r science
repository.

WHAT LIVES HERE, AND WHY IT IS A COPY, NOT AN IMPORT
-----------------------------------------------------
sdr_core.py, waterfalls.py, iq_dashboard.py, band_correlator.py, physics.py
and console.py are vendored (copied) from 01_CODE/ in the sdr_r science repo,
not imported from it. That is a deliberate compromise, not the preferred
outcome:

  * sdr_r and SReTo are two separate git repositories with independent
    histories, published separately on GitHub. A `pip install sreto` has no
    access to sdr_r's 01_CODE/ at all — there is nothing on the far end of an
    `import sdr_core` for a fresh clone to resolve. Vendoring is what makes
    the package in this repo's own words ("everything works as intended")
    true for someone who only ever cloned SReTo.
  * The alternative — sreto reaching across the filesystem into a configured
    science repo's 01_CODE/ and importing sdr_core from there, the same way
    sreto._lazy_orbits.py already does for orbits.py — was considered and
    rejected for THIS code specifically: orbits.py is read-only geometry math
    the GUI merely displays, but this package's whole purpose is to be the
    thing a bare `pip install sreto[analysis]` can run with NO science repo
    present at all (see sreto.analysis.paths' self-contained fallback). That
    property is incompatible with requiring an external repo to import from.

HOW THE COPIES STAY IN SYNC
----------------------------
Every vendored file carries a "VENDORED COPY" note in its module docstring
naming its sdr_r source file and listing EVERY line that differs from it
(import statements only — see waterfalls.py / iq_dashboard.py /
band_correlator.py / physics.py's own docstrings for the exact diff).
sdr_core.py and console.py have zero local imports and are byte-identical to
their sdr_r originals.

physics.py was vendored later than the rest, when the Physics tab was added.
It differs from 01_CODE/physics.py by exactly one line (`from . import
sdr_core`) plus its own vendoring note, and is verified by running its
built-in known-truth suite against the copy:

    PYTHONPATH=src python -m sreto.analysis.physics --selftest

This was verified at port time (see the parity check below) against sdr_r
commit 9536cb2 (2026-07-28) plus this session's own uncommitted fixes to
iq_dashboard.py/band_correlator.py's `save_dir` default (removed a hardcoded
`/Users/user/...` fallback — see those files' own history).

THERE IS NO AUTOMATION KEEPING THEM IN SYNC. This is the honest weak point of
vendoring: a future change to sdr_r's DSP files does not propagate here by
itself. Whoever changes the maths in sdr_r's sdr_core.py / waterfalls.py /
iq_dashboard.py / band_correlator.py must re-copy the four files (`cp
01_CODE/X.py src/sreto/analysis/X.py`, then reapply the import-line diff
documented in each file's docstring) as part of that change, the same way a
vendored dependency in any other project is bumped by hand. A test asserting
"file X matches its sdr_r source" was deliberately NOT added, because it
would need a checkout of sdr_r's path baked into SReTo's test suite — which
is precisely the coupling vendoring exists to avoid. The guard that DOES
exist is numerical: both trees' self-tests (iq_dashboard --selftest,
band_correlator --selftest) inject a known beacon/delay and assert on the
recovered value, so a drift big enough to matter fails loudly in whichever
tree it happens in.

WHAT IS DELIBERATELY NOT HERE
------------------------------
physics.py, orbits.py, fresnel.py, satellites.py, soop_planner.py, and
MAIN.py itself are not ported. physics.py is being actively rewritten by
another agent in sdr_r right now — porting a snapshot of it here would be
stale before this sentence is read. The other four are geometry/satellite
infrastructure outside this port's scope (sdr_core / waterfalls /
iq_dashboard / band_correlator / repo_paths / paths.env only, per the task
that produced this package). MAIN.py's role — reading the USER PARAMETERS
block, resolving the satellite/geometry, orchestrating all of the above in
one script — has no equivalent here; see the "packaging decision" note in
each module for what calling this package directly looks like instead.

HOW THIS IS WIRED INTO THE GUI (or rather: not, yet)
------------------------------------------------------
sreto.panels.analysis_panel still shells out to the CONFIGURED SCIENCE
REPO's MAIN.py (via sreto.jobs.analysis_job), unchanged by this port. That
remains the only way to get the FULL pipeline — physics/soil-moisture,
satellite geometry, Fresnel footprints — because those stages were
deliberately not ported here. This package is the standalone, science-repo-
independent core (waterfalls, IQ dashboard, band correlator) that a future
change could wire the Analysis tab to call directly instead of shelling out,
but that rewiring was not made part of this port: it would change the tab's
behaviour when a science repo IS configured (today it runs physics too), and
that is a product decision, not a path-plumbing one.
"""
