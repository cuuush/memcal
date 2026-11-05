# Full Disk Access helper

On macOS, local Messages and WhatsApp reads may require Full Disk Access. Calendar
and Automation consent are separate. Use the helper from your normal installation:

```bash
memcal permissions status       # read checks only
memcal permissions setup        # guided settings and app reveal
```

The same helper appears in **Settings → Full Disk Access** in the web UI. It shows
the app to enable, explains the approval and restart steps, and has a **Recheck source
access** button. Opening the helper does not enable a privacy switch. You complete
any macOS confirmation yourself. Full Disk Access covers other apps' protected data
as well as the local sources enabled in Memcal.

## Choose the correct requester

The helper tests the current interactive process and a fresh scheduled Memcal app
process independently. Both checks read a constant from enabled source tables, never
message bodies or the private macOS permission database.

- If the scheduled app cannot read, it guides you to `~/.memcal/memcal.app`. If the
  bundle is missing, explicit setup prepares it without installing a nightly job.
- If scheduled reads work but interactive reads fail, it identifies the enclosing
  terminal or agent app from process paths and guides you to that app. This can happen
  even when the command passes through the Memcal wrapper. Restart the terminal/agent
  after changing its grant.
- If both can read, the helper reports success and does not request the grant again.
  Missing databases and failed probes remain unverified rather than proving access.

You can choose a requester explicitly or preview the plan without opening anything:

```bash
memcal permissions setup --target memcal
memcal permissions setup --target host
memcal permissions setup --no-open --json
```

When a host app cannot be identified, the helper says so and gives manual steps.
It never guesses an executable to authorize. If the direct settings link fails, it
opens System Settings and retains the navigation instructions.

After granting access, run `memcal permissions status` again. A new probe verifies
the scheduled requester; restarting the interactive app may be necessary for its
new grant. Status checks and setup guidance do not open or migrate the Memcal database,
ingest sources, or call a model. The web helper works even if that database cannot open.
On Linux, the command explains that this is a macOS setting and opens nothing.

Apple's [Full Disk Access announcement](https://developer.apple.com/news/?id=p6zjojqw)
describes additional controls requiring explicit user action. Memcal leaves that
approval in the system interface and verifies actual source reads afterward.
