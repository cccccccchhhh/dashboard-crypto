// Vercel Cron -> asks GitHub to run the "scan-divergence" workflow (workflow_dispatch).
// GitHub's own `schedule:` trigger is often hours late or skipped; Vercel Cron is much more punctual, so it does the waking up and
// GitHub Actions still does the actual scan (Python) and the commit.
//
// Environment variables (Vercel project settings):
//   GITHUB_TOKEN  fine-grained personal access token, only this repository, permission "Actions: Read and write"
//   CRON_SECRET   any long random string. Vercel sends it as "Authorization: Bearer <secret>" on cron calls, which also keeps
//                 strangers from triggering the endpoint.
//   GITHUB_REPO   optional, "owner/name" (default below)
module.exports = async (req, res) => {
  const secret = process.env.CRON_SECRET;
  if (!secret) return res.status(500).json({ ok: false, error: "CRON_SECRET is not set" });
  if (req.headers.authorization !== `Bearer ${secret}`) return res.status(401).json({ ok: false, error: "unauthorized" });
  const token = process.env.GITHUB_TOKEN;
  if (!token) return res.status(500).json({ ok: false, error: "GITHUB_TOKEN is not set" });
  const repo = process.env.GITHUB_REPO || "cccccccchhhh/dashboard-crypto";
  const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/scan.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "divergence-cron",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (r.status === 204) return res.status(200).json({ ok: true, triggered_at: new Date().toISOString() });
  const body = await r.text();
  return res.status(502).json({ ok: false, github_status: r.status, body: body.slice(0, 300) });
};
