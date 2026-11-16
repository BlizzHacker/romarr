<?php
declare(strict_types=1);

/**
 * Open Collective webhook receiver & supporter integration endpoint for ROMarr.
 *
 * URL: https://romarr.app/opc
 * Receives Open Collective JSON activity webhooks (POST), verifies payload identity
 * against the ROMarr collective (legacyId 1349706 / slug "romarr"), persists a bounded
 * sanitized activity feed in private storage, forwards verified events to configured
 * Discord/ROMarr targets, and renders a live status page + JSON feed on GET.
 */

const OC_SLUG = 'romarr';
const OC_LEGACY_ID = 1349706;
const OC_URL = 'https://opencollective.com/romarr';
const OC_WEBHOOKS_DASHBOARD = 'https://opencollective.com/dashboard/romarr/webhooks';
const OC_GRAPHQL_URL = 'https://api.opencollective.com/graphql/v2';
const DISCORD_URL = 'https://discord.gg/ywNFceuAd';
const GITHUB_URL = 'https://github.com/BlizzHacker/romarr';
const MAX_BODY_BYTES = 524288;
const MAX_EVENTS = 50;
const GRAPHQL_CACHE_TTL = 300;

function opc_private_dir(): string {
    $candidates = [
        '/home/admin/web/romarr.app/private',
        dirname(__DIR__, 2) . '/private',
        sys_get_temp_dir() . '/romarr-opc',
    ];
    foreach ($candidates as $dir) {
        if (is_dir($dir) && is_writable($dir)) {
            return $dir;
        }
    }
    $fallback = sys_get_temp_dir() . '/romarr-opc';
    if (!is_dir($fallback)) {
        @mkdir($fallback, 0750, true);
    }
    return $fallback;
}

function opc_read_json(string $path, array $default = []): array {
    if (!is_file($path)) {
        return $default;
    }
    $raw = @file_get_contents($path);
    if (!is_string($raw) || $raw === '') {
        return $default;
    }
    $decoded = json_decode($raw, true);
    return is_array($decoded) ? $decoded : $default;
}

function opc_write_json(string $path, array $payload): bool {
    $encoded = json_encode($payload, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
    if (!is_string($encoded)) {
        return false;
    }
    $tmp = $path . '.tmp.' . bin2hex(random_bytes(4));
    if (@file_put_contents($tmp, $encoded, LOCK_EX) === false) {
        return false;
    }
    @chmod($tmp, 0640);
    return @rename($tmp, $path);
}

function opc_header(string $name): string {
    $key = 'HTTP_' . strtoupper(str_replace('-', '_', $name));
    return trim((string)($_SERVER[$key] ?? ''));
}

function opc_bearer_token(): string {
    $auth = opc_header('Authorization');
    if (stripos($auth, 'Bearer ') === 0) {
        return trim(substr($auth, 7));
    }
    $hdr = opc_header('X-ROMarr-Webhook-Token');
    if ($hdr !== '') {
        return $hdr;
    }
    return trim((string)($_GET['token'] ?? ''));
}

function opc_format_cents($cents, string $currency = 'USD'): string {
    if (!is_int($cents) && !is_float($cents)) {
        return '';
    }
    $curr = strtoupper(trim($currency ?: 'USD'));
    $prefix = $curr === 'USD' ? '$' : ($curr . ' ');
    return $prefix . number_format(((float)$cents) / 100.0, 2);
}

function opc_format_obj_amount(array $obj): string {
    foreach (['formattedAmountWithInterval', 'formattedAmount'] as $k) {
        $v = trim((string)($obj[$k] ?? ''));
        if ($v !== '') {
            return $v;
        }
    }
    $cents = $obj['totalAmount'] ?? $obj['amount'] ?? null;
    if (is_int($cents) || is_float($cents)) {
        $currency = (string)($obj['currency'] ?? 'USD');
        $text = opc_format_cents($cents, $currency);
        $interval = strtolower(trim((string)($obj['interval'] ?? '')));
        return $interval !== '' ? ($text . ' / ' . $interval) : $text;
    }
    return '';
}

function opc_http_json_post(string $url, array $payload, array $headers = [], int $timeout = 8): array {
    $json = json_encode($payload, JSON_UNESCAPED_SLASHES);
    if (!is_string($json)) {
        return ['ok' => false, 'status' => 0, 'error' => 'json encode error'];
    }
    $hdrs = array_merge([
        'Content-Type: application/json',
        'User-Agent: ROMarr-OpenCollective-Bridge/1.0 (+https://romarr.app/opc)',
    ], $headers);
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $json,
            CURLOPT_HTTPHEADER => $hdrs,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => 4,
            CURLOPT_TIMEOUT => $timeout,
            CURLOPT_FOLLOWLOCATION => false,
        ]);
        $body = curl_exec($ch);
        $status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $err = curl_error($ch);
        curl_close($ch);
        return ['ok' => $status >= 200 && $status < 300, 'status' => $status, 'error' => $err, 'body' => is_string($body) ? $body : ''];
    }
    $ctx = stream_context_create([
        'http' => [
            'method' => 'POST',
            'header' => implode("\r\n", $hdrs),
            'content' => $json,
            'timeout' => $timeout,
            'ignore_errors' => true,
        ],
    ]);
    $body = @file_get_contents($url, false, $ctx);
    $status = 0;
    if (isset($http_response_header[0]) && preg_match('/\s(\d{3})\b/', $http_response_header[0], $m)) {
        $status = (int)$m[1];
    }
    return ['ok' => $status >= 200 && $status < 300, 'status' => $status, 'error' => '', 'body' => is_string($body) ? $body : ''];
}

function opc_fetch_collective_stats(string $privateDir): array {
    $cachePath = $privateDir . '/opc-cache.json';
    $cached = opc_read_json($cachePath, []);
    $now = time();
    if (!empty($cached['fetched_at']) && ($now - (int)$cached['fetched_at']) < GRAPHQL_CACHE_TTL && !empty($cached['account'])) {
        return $cached['account'];
    }
    $query = '{ account(slug: "romarr") { id legacyId name slug description website stats { balance { valueInCents currency } totalAmountReceived { valueInCents currency } contributorsCount } ... on Collective { tiers { nodes { id name slug description amount { valueInCents currency } interval } } } } }';
    $res = opc_http_json_post(OC_GRAPHQL_URL, ['query' => $query], ['Accept: application/json'], 6);
    if ($res['ok'] && $res['body'] !== '') {
        $data = json_decode($res['body'], true);
        if (is_array($data['data']['account'] ?? null)) {
            $account = $data['data']['account'];
            opc_write_json($cachePath, ['fetched_at' => $now, 'account' => $account]);
            return $account;
        }
    }
    return is_array($cached['account'] ?? null) ? $cached['account'] : [
        'id' => 'acc_WrijCbGTmihbEuEVfhLRd',
        'legacyId' => OC_LEGACY_ID,
        'name' => 'ROMarr',
        'slug' => OC_SLUG,
        'description' => 'Giving everyone games.',
        'stats' => [
            'balance' => ['valueInCents' => 0, 'currency' => 'USD'],
            'totalAmountReceived' => ['valueInCents' => 0, 'currency' => 'USD'],
            'contributorsCount' => 0,
        ],
        'tiers' => ['nodes' => []],
    ];
}

function opc_parse_event(array $body): ?array {
    $type = strtoupper(trim((string)($body['type'] ?? '')));
    if ($type === '') {
        return null;
    }
    $data = is_array($body['data'] ?? null) ? $body['data'] : [];
    $collective = is_array($data['collective'] ?? null) ? $data['collective'] : [];
    $slug = strtolower(trim((string)($collective['slug'] ?? '')));
    $collectiveId = $body['CollectiveId'] ?? ($collective['id'] ?? null);

    // Ensure payload belongs to the ROMarr Open Collective account when identity fields are present
    if ($collectiveId !== null && (int)$collectiveId !== OC_LEGACY_ID && $slug !== OC_SLUG) {
        return null;
    }
    if ($slug !== '' && $slug !== OC_SLUG) {
        return null;
    }

    $from = is_array($data['fromCollective'] ?? null) ? $data['fromCollective'] : [];
    $member = is_array($data['member'] ?? null) ? $data['member'] : [];
    $memberCol = is_array($member['memberCollective'] ?? null) ? $member['memberCollective'] : [];
    $order = is_array($data['order'] ?? null) ? $data['order'] : [];
    $tx = is_array($data['transaction'] ?? null) ? $data['transaction'] : [];
    $expense = is_array($data['expense'] ?? null) ? $data['expense'] : [];
    $update = is_array($data['update'] ?? null) ? $data['update'] : [];
    $tier = is_array($data['tier'] ?? null) ? $data['tier'] : (is_array($member['tier'] ?? null) ? $member['tier'] : []);
    $recipient = is_array($data['recipient'] ?? null) ? $data['recipient'] : [];

    $actor = trim((string)($from['name'] ?? $memberCol['name'] ?? $recipient['name'] ?? 'A supporter'));
    if ($actor === '') {
        $actor = 'A supporter';
    }
    $tierName = trim((string)($tier['name'] ?? ''));
    $amount = opc_format_obj_amount($order) ?: opc_format_obj_amount($tx) ?: opc_format_obj_amount($expense);
    $updateTitle = trim((string)($update['title'] ?? ''));

    $summary = match ($type) {
        'ORDER_PROCESSED', 'COLLECTIVE_TRANSACTION_CREATED' => trim($actor . ($amount !== '' ? (' contributed ' . $amount) : ' sent a contribution') . ($tierName !== '' ? (' (' . $tierName . ')') : '')),
        'ORDER_PENDING_CREATED' => trim('Pending contribution from ' . $actor . ($amount !== '' ? (' (' . $amount . ')') : '')),
        'COLLECTIVE_MEMBER_CREATED' => trim($actor . ' joined ROMarr' . ($tierName !== '' ? (' — ' . $tierName) : '')),
        'COLLECTIVE_UPDATE_PUBLISHED', 'COLLECTIVE_UPDATE_CREATED' => 'Update published: ' . ($updateTitle !== '' ? $updateTitle : 'ROMarr Collective Update'),
        'SUBSCRIPTION_CANCELED', 'SUBSCRIPTION_PAUSED', 'SUBSCRIPTION_RESUMED' => trim('Subscription ' . strtolower(substr($type, 13)) . ' for ' . $actor),
        default => str_starts_with($type, 'COLLECTIVE_EXPENSE_')
            ? trim('Expense ' . strtolower(substr($type, 19)) . ($amount !== '' ? (' (' . $amount . ')') : ''))
            : ('Open Collective event: ' . strtolower(str_replace('_', ' ', $type))),
    };

    return [
        'id' => $body['id'] ?? null,
        'type' => $type,
        'collective_id' => $collectiveId ?? OC_LEGACY_ID,
        'collective_slug' => OC_SLUG,
        'actor' => $actor,
        'tier' => $tierName,
        'amount' => $amount,
        'summary' => $summary,
        'created_at' => (string)($body['createdAt'] ?? gmdate('c')),
        'received_at' => gmdate('c'),
    ];
}

$privateDir = opc_private_dir();
$configPath = $privateDir . '/opc.json';
$eventsPath = $privateDir . '/opc-events.json';
$cfg = opc_read_json($configPath, []);

if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'POST') {
    header('Content-Type: application/json; charset=utf-8');
    $expectedToken = trim((string)($cfg['webhook_token'] ?? ''));
    $requireToken = !empty($cfg['require_token']);
    $providedToken = opc_bearer_token();
    if ($expectedToken !== '' && ($requireToken || $providedToken !== '') && !hash_equals($expectedToken, $providedToken)) {
        http_response_code(401);
        echo json_encode(['ok' => false, 'error' => 'invalid webhook token']);
        exit;
    }

    $raw = file_get_contents('php://input', false, null, 0, MAX_BODY_BYTES + 1);
    if (!is_string($raw) || strlen($raw) === 0 || strlen($raw) > MAX_BODY_BYTES) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'invalid or oversized payload']);
        exit;
    }

    $decoded = json_decode($raw, true);
    if (!is_array($decoded)) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'error' => 'JSON object expected']);
        exit;
    }

    $parsed = opc_parse_event($decoded);
    if ($parsed === null) {
        http_response_code(422);
        echo json_encode(['ok' => false, 'error' => 'payload does not match the ROMarr Open Collective webhook contract']);
        exit;
    }

    $events = opc_read_json($eventsPath, []);
    $isDuplicate = false;
    if ($parsed['id'] !== null) {
        foreach ($events as $existing) {
            if (($existing['id'] ?? null) === $parsed['id'] && ($existing['type'] ?? '') === $parsed['type']) {
                $isDuplicate = true;
                break;
            }
        }
    }

    $forwarded = [];
    if (!$isDuplicate) {
        array_unshift($events, $parsed);
        $events = array_slice($events, 0, MAX_EVENTS);
        opc_write_json($eventsPath, $events);
        @unlink($privateDir . '/opc-cache.json');

        $romarrUrl = trim((string)($cfg['romarr_webhook_url'] ?? ''));
        $romarrKey = trim((string)($cfg['romarr_api_key'] ?? ''));
        if ($romarrUrl !== '' && $romarrKey !== '') {
            $fwdRes = opc_http_json_post($romarrUrl, $decoded, ['X-Api-Key: ' . $romarrKey], 5);
            $forwarded['romarr'] = ['ok' => $fwdRes['ok'], 'status' => $fwdRes['status']];
        }

        $discordHook = trim((string)($cfg['discord_webhook_url'] ?? ''));
        if ($discordHook !== '') {
            $embed = [
                'username' => 'ROMarr · Open Collective',
                'allowed_mentions' => ['parse' => []],
                'embeds' => [[
                    'title' => $parsed['summary'],
                    'url' => OC_URL,
                    'color' => 0x75ADFF,
                    'fields' => array_values(array_filter([
                        ['name' => 'Event', 'value' => $parsed['type'], 'inline' => true],
                        $parsed['amount'] !== '' ? ['name' => 'Amount', 'value' => $parsed['amount'], 'inline' => true] : null,
                        $parsed['tier'] !== '' ? ['name' => 'Tier', 'value' => $parsed['tier'], 'inline' => true] : null,
                    ])),
                    'footer' => ['text' => 'romarr.app/opc · Open Collective'],
                    'timestamp' => $parsed['created_at'],
                ]],
            ];
            $discRes = opc_http_json_post($discordHook, $embed, [], 5);
            $forwarded['discord'] = ['ok' => $discRes['ok'], 'status' => $discRes['status']];
        }
    }

    http_response_code(202);
    echo json_encode([
        'ok' => true,
        'accepted' => true,
        'duplicate' => $isDuplicate,
        'event' => $parsed,
        'forwarded' => $forwarded,
    ], JSON_UNESCAPED_SLASHES);
    exit;
}

$statsAccount = opc_fetch_collective_stats($privateDir);
$recentEvents = opc_read_json($eventsPath, []);
$wantsJson = (($_GET['format'] ?? '') === 'json')
    || str_contains(strtolower(opc_header('Accept')), 'application/json');

if ($wantsJson) {
    header('Cache-Control: no-store, max-age=0');
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode([
        'ok' => true,
        'service' => 'romarr-opencollective-webhook',
        'endpoint' => 'https://romarr.app/opc',
        'opencollective' => OC_URL,
        'discord' => DISCORD_URL,
        'github' => GITHUB_URL,
        'collective' => $statsAccount,
        'recent_events' => array_slice($recentEvents, 0, 15),
    ], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
    exit;
}
header('Cache-Control: no-store, max-age=0');

$stats = is_array($statsAccount['stats'] ?? null) ? $statsAccount['stats'] : [];
$balance = opc_format_cents($stats['balance']['valueInCents'] ?? 0, (string)($stats['balance']['currency'] ?? 'USD'));
$received = opc_format_cents($stats['totalAmountReceived']['valueInCents'] ?? 0, (string)($stats['totalAmountReceived']['currency'] ?? 'USD'));
$contributors = (int)($stats['contributorsCount'] ?? 0);
$tiers = is_array($statsAccount['tiers']['nodes'] ?? null) ? $statsAccount['tiers']['nodes'] : [];
$h = static fn(string $v): string => htmlspecialchars($v, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
?>
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#080b10">
  <title>ROMarr · Open Collective &amp; Community Bridge</title>
  <link rel="icon" href="/assets/mark.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/styles.css?v=romarr-blue-workbench-7">
  <style>
    .opc-shell{max-width:980px;margin:0 auto;padding:42px 24px 64px}
    .opc-card{background:var(--surface,#101720);border:1px solid var(--line,#273341);border-radius:12px;padding:24px;margin-top:20px}
    .opc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-top:16px}
    .opc-stat{background:var(--bg-raised,#0d1219);border:1px solid var(--line,#273341);border-radius:10px;padding:16px}
    .opc-stat b{display:block;font-size:24px;color:var(--blue,#75adff);margin-bottom:4px}
    .opc-stat span{color:var(--muted,#b1bdca);font-size:13px}
    .opc-code{display:block;padding:12px 14px;background:var(--bg,#080b10);border:1px solid var(--line,#273341);border-radius:8px;font-family:var(--mono,monospace);font-size:13px;overflow-x:auto;color:var(--ink,#eff3f8)}
    .opc-list{list-style:none;padding:0;margin:12px 0 0}
    .opc-list li{padding:10px 0;border-top:1px solid var(--line,#273341);display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:14px}
    .opc-pill{display:inline-block;padding:3px 9px;border-radius:999px;border:1px solid var(--line-bright,#394a5c);font-size:12px;color:var(--blue,#75adff)}
  </style>
</head>
<body>
  <header class="site-header">
    <div class="wrap header-inner">
      <a class="brand" href="/">
        <span class="brand-mark" aria-hidden="true"><img src="/assets/romarr-icon.svg" alt="" width="34" height="31"></span>
        <span class="brand-word">ROM<span>arr</span></span>
        <span class="brand-divider" aria-hidden="true"></span>
        <span class="brand-note">Open Collective &amp; Webhook Bridge</span>
      </a>
      <div class="hero-actions" style="margin:0;gap:10px">
        <a class="button button-outline" href="<?= $h(DISCORD_URL) ?>" target="_blank" rel="noopener noreferrer">Discord ↗</a>
        <a class="button button-primary" href="<?= $h(OC_URL) ?>" target="_blank" rel="noopener noreferrer">Sponsor on Open Collective ↗</a>
      </div>
    </div>
  </header>

  <main class="opc-shell">
    <p class="section-kicker">OPENCOLLECTIVE · DISCORD · GITHUB · ROMARR</p>
    <h1 style="margin:8px 0 12px;font-size:clamp(28px,4vw,42px)">ROMarr Community &amp; Webhook Hub</h1>
    <p style="color:var(--muted);max-width:72ch">Live webhook receiver and supporter bridge for <strong><?= $h((string)($statsAccount['name'] ?? 'ROMarr')) ?></strong> on Open Collective, wired to Discord and self-hosted ROMarr instances.</p>

    <div class="opc-card">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
        <h2 style="margin:0">Open Collective Snapshot</h2>
        <img src="https://opencollective.com/romarr/tiers/badge.svg" alt="Open Collective financial contributors badge">
      </div>
      <div class="opc-grid">
        <div class="opc-stat"><b><?= $h($balance) ?></b><span>Current Balance</span></div>
        <div class="opc-stat"><b><?= $h($received) ?></b><span>Total Raised</span></div>
        <div class="opc-stat"><b><?= $contributors ?></b><span>Financial Contributors</span></div>
        <div class="opc-stat"><b>#<?= OC_LEGACY_ID ?></b><span>Collective ID (<code>romarr</code>)</span></div>
      </div>
      <div class="hero-actions" style="margin-top:18px">
        <a class="button button-primary" href="<?= $h(OC_URL) ?>" target="_blank" rel="noopener noreferrer">Open Collective Page ↗</a>
        <a class="button button-outline" href="<?= $h(DISCORD_URL) ?>" target="_blank" rel="noopener noreferrer">Join ROMarr Discord ↗</a>
        <a class="button button-outline" href="<?= $h(GITHUB_URL) ?>" target="_blank" rel="noopener noreferrer">GitHub Repository ↗</a>
      </div>
    </div>

    <div class="opc-card">
      <h2 style="margin-top:0">Webhook Endpoint Configuration</h2>
      <p style="color:var(--muted)">In <a href="<?= $h(OC_WEBHOOKS_DASHBOARD) ?>" target="_blank" rel="noopener noreferrer">Open Collective Webhooks Settings ↗</a>, point a webhook directly to this receiver or bridge directly to your own ROMarr instance:</p>
      <code class="opc-code">https://romarr.app/opc</code>
      <p style="color:var(--muted);margin-top:12px">Self-hosted ROMarr instances also expose an authenticated inbound endpoint at <code>POST /api/v1/webhook/opencollective?apikey=&lt;KEY&gt;</code> that records supporter activity in History and fans out notifications to Discord, Slack, ntfy, Gotify, Telegram, Pushover, or Apprise.</p>
      <?php if (!empty($tiers)): ?>
      <h3 style="margin-top:18px">Active Tiers</h3>
      <ul class="opc-list">
        <?php foreach ($tiers as $t): ?>
        <li>
          <span><strong><?= $h((string)($t['name'] ?? 'Tier')) ?></strong> — <?= $h((string)($t['description'] ?? '')) ?></span>
          <span class="opc-pill"><?= $h(opc_format_obj_amount($t['amount'] ?? [])) ?></span>
        </li>
        <?php endforeach; ?>
      </ul>
      <?php endif; ?>
    </div>

    <div class="opc-card">
      <h2 style="margin-top:0">Recent Verified Webhook Activity</h2>
      <?php if (empty($recentEvents)): ?>
        <p style="color:var(--muted);margin-bottom:0">Listening for Open Collective events (<code>ORDER_PROCESSED</code>, <code>COLLECTIVE_MEMBER_CREATED</code>, <code>COLLECTIVE_UPDATE_PUBLISHED</code>, <code>COLLECTIVE_EXPENSE_*</code>, <code>SUBSCRIPTION_*</code>).</p>
      <?php else: ?>
        <ul class="opc-list">
          <?php foreach (array_slice($recentEvents, 0, 10) as $ev): ?>
          <li>
            <span><strong><?= $h((string)($ev['summary'] ?? $ev['type'] ?? 'Event')) ?></strong></span>
            <span class="opc-pill"><?= $h((string)($ev['type'] ?? '')) ?> · <?= $h((string)($ev['received_at'] ?? '')) ?></span>
          </li>
          <?php endforeach; ?>
        </ul>
      <?php endif; ?>
    </div>
  </main>
</body>
</html>
