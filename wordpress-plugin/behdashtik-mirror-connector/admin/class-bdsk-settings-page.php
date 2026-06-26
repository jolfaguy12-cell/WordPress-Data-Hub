<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_Settings_Page {

	private const PAGE_SLUG = 'bdsk-settings';

	public static function init(): void {
		add_action( 'admin_menu',    [ __CLASS__, 'add_menu' ] );
		add_action( 'admin_init',    [ __CLASS__, 'maybe_redirect_after_activation' ] );
		add_action( 'admin_notices', [ __CLASS__, 'maybe_show_notice' ] );

		add_action( 'admin_post_bdsk_save_security',       [ __CLASS__, 'handle_save_security' ] );
		add_action( 'admin_post_bdsk_save_sync',           [ __CLASS__, 'handle_save_sync' ] );
		add_action( 'admin_post_bdsk_generate_key',        [ __CLASS__, 'handle_generate_key' ] );
		add_action( 'admin_post_bdsk_rebuild_media_index', [ __CLASS__, 'handle_rebuild_media_index' ] );
		// admin_post_bdsk_emergency_cleanup is handled by BDSK_Cleanup::init()
		add_action( 'admin_post_bdsk_reset_counters',      [ __CLASS__, 'handle_reset_counters' ] );
	}

	// ---------------------------------------------------------------------------
	// Activation redirect
	// ---------------------------------------------------------------------------

	public static function maybe_redirect_after_activation(): void {
		if ( ! get_transient( 'bdsk_activation_redirect' ) ) {
			return;
		}
		delete_transient( 'bdsk_activation_redirect' );
		if ( ! is_network_admin() && ! isset( $_GET['activate-multi'] ) ) {
			wp_safe_redirect( admin_url( 'options-general.php?page=' . self::PAGE_SLUG ) );
			exit;
		}
	}

	// ---------------------------------------------------------------------------
	// Menu
	// ---------------------------------------------------------------------------

	public static function add_menu(): void {
		add_options_page(
			'Behdashtik Mirror Connector',
			'Behdashtik Mirror',
			'manage_options',
			self::PAGE_SLUG,
			[ __CLASS__, 'render' ]
		);
	}

	// ---------------------------------------------------------------------------
	// Admin notices
	// ---------------------------------------------------------------------------

	public static function maybe_show_notice(): void {
		$storage_error = BDSK_Export_Job::export_storage_error();
		if ( null !== $storage_error ) {
			echo '<div class="notice notice-error"><p><strong>Behdashtik Mirror Connector — Export Storage Error:</strong> '
				. esc_html( $storage_error )
				. ' Export is disabled until this is resolved. Set <code>define(\'BDSK_EXPORT_STORAGE_PATH\', \'/path/outside/webroot/bdsk-private\');</code> in <code>wp-config.php</code>.</p></div>';
		}

		if ( BDSK_Settings::get( 'disable_ip_check' ) && BDSK_Settings::get( 'enabled' ) ) {
			echo '<div class="notice notice-warning"><p><strong>Behdashtik Mirror Connector:</strong> IP check is disabled. Only use this in local development.</p></div>';
		}

		$screen = get_current_screen();
		if ( ! $screen || 'settings_page_' . self::PAGE_SLUG !== $screen->id ) {
			return;
		}

		// phpcs:ignore WordPress.Security.NonceVerification.Recommended
		switch ( $_GET['bdsk_notice'] ?? '' ) {
			case 'saved':
				echo '<div class="notice notice-success is-dismissible"><p><strong>Behdashtik Mirror Connector:</strong> Settings saved.</p></div>';
				break;
			case 'cleanup_done':
				echo '<div class="notice notice-success is-dismissible"><p><strong>Behdashtik Mirror Connector:</strong> Emergency cleanup completed.</p></div>';
				break;
			case 'counters_reset':
				echo '<div class="notice notice-success is-dismissible"><p><strong>Behdashtik Mirror Connector:</strong> Counters reset and request log cleared.</p></div>';
				break;
			case 'media_rebuild_started':
				echo '<div class="notice notice-success is-dismissible"><p><strong>Behdashtik Mirror Connector:</strong> Media index rebuild started in the background.</p></div>';
				break;
			case 'media_rebuild_error':
				echo '<div class="notice notice-error is-dismissible"><p><strong>Behdashtik Mirror Connector:</strong> Could not start media index rebuild (already running or Action Scheduler unavailable).</p></div>';
				break;
		}
	}

	// ---------------------------------------------------------------------------
	// Tab navigation helper
	// ---------------------------------------------------------------------------

	private static function current_tab(): string {
		// phpcs:ignore WordPress.Security.NonceVerification.Recommended
		return sanitize_key( $_GET['tab'] ?? 'dashboard' );
	}

	private static function tab_url( string $tab ): string {
		return admin_url( 'options-general.php?page=' . self::PAGE_SLUG . '&tab=' . $tab );
	}

	private static function render_tabs(): void {
		$current = self::current_tab();
		$tabs    = [
			'dashboard' => 'Dashboard',
			'security'  => 'Security',
			'sync'      => 'Sync Settings',
			'history'   => 'Job History',
			'danger'    => 'Danger Zone',
		];
		echo '<nav class="nav-tab-wrapper" style="margin-bottom:24px">';
		foreach ( $tabs as $slug => $label ) {
			$class = ( $slug === $current ) ? 'nav-tab nav-tab-active' : 'nav-tab';
			printf(
				'<a href="%s" class="%s">%s</a>',
				esc_url( self::tab_url( $slug ) ),
				esc_attr( $class ),
				esc_html( $label )
			);
		}
		echo '</nav>';
	}

	// ---------------------------------------------------------------------------
	// Main render dispatcher
	// ---------------------------------------------------------------------------

	public static function render(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised.' );
		}

		$has_secret      = BDSK_Security::has_secret();
		$using_constant  = BDSK_Security::using_constant();
		$using_encrypted = BDSK_Security::using_encrypted_option();
		$openssl_ok      = extension_loaded( 'openssl' );

		echo '<div class="wrap"><h1>Behdashtik Mirror Connector</h1>';

		// Alerts
		if ( ! $has_secret && ! $using_constant ) {
			echo '<div class="notice notice-error"><p><strong>No API key configured.</strong> Generate one on the Security tab.</p></div>';
		}
		if ( $using_encrypted && ! $openssl_ok ) {
			echo '<div class="notice notice-error"><p><strong>OpenSSL extension not available.</strong> The stored API key cannot be decrypted.</p></div>';
		}

		// Show-once new-key flash (shown above tabs so it survives any redirect)
		$flash_key = get_transient( 'bdsk_flash_new_key' );
		if ( $flash_key ) {
			delete_transient( 'bdsk_flash_new_key' );
			?>
			<div class="notice notice-warning" style="padding:16px">
				<p><strong>&#x26A0; Save this API key now — it will not be shown again.</strong></p>
				<p>Copy it into <code>config.json</code> on Server 2 as the <code>api_secret</code> value.</p>
				<div style="display:flex;gap:8px;align-items:center;margin-top:8px">
					<input type="text" id="bdsk-new-key" value="<?php echo esc_attr( $flash_key ); ?>"
					       readonly style="font-family:monospace;width:520px;font-size:13px" />
					<button type="button"
					        onclick="navigator.clipboard.writeText(document.getElementById('bdsk-new-key').value).then(function(){this.textContent='Copied!';}.bind(this))"
					        class="button">Copy</button>
				</div>
			</div>
			<?php
		}

		self::render_tabs();

		switch ( self::current_tab() ) {
			case 'security':
				self::render_security_tab();
				break;
			case 'sync':
				self::render_sync_tab();
				break;
			case 'history':
				self::render_history_tab();
				break;
			case 'danger':
				self::render_danger_tab();
				break;
			default:
				self::render_dashboard_tab();
		}

		echo '</div>';
	}

	// ---------------------------------------------------------------------------
	// Dashboard tab
	// ---------------------------------------------------------------------------

	private static function render_dashboard_tab(): void {
		$d = BDSK_Stats::get_dashboard_data();
		$c = $d['connector'];
		$r = $d['requests'];
		$e = $d['exports'];
		$m = $d['media'];
		$v = $d['events'];
		$cl = $d['cleanup'];

		// Connection badge
		$conn_badge = match( $c['last_connection']['status'] ) {
			'connected' => '<span style="color:green;font-weight:600">&#x25CF; Connected</span>',
			'stale'     => '<span style="color:orange;font-weight:600">&#x25CF; Stale</span>',
			default     => '<span style="color:#646970;font-weight:600">&#x25CB; Never connected</span>',
		};

		// Non-zero rejection reasons only
		$reasons_html = '';
		foreach ( $r['by_reason'] as $reason => $cnt ) {
			if ( $cnt > 0 ) {
				$reasons_html .= '<tr><td>' . esc_html( $reason ) . '</td><td>' . (int) $cnt . '</td></tr>';
			}
		}

		$card = static function ( string $title, string $body ): void {
			echo '<div class="card" style="max-width:860px;margin-bottom:20px"><h2 style="margin-top:0">' . esc_html( $title ) . '</h2>' . $body . '</div>';
		};

		// -- Connector card --
		ob_start();
		?>
		<table class="widefat striped" style="width:auto">
			<tr><th>Status</th><td><?php echo wp_kses( $conn_badge, [ 'span' => [ 'style' => [] ] ] ); ?></td></tr>
			<tr><th>Last successful request</th><td><?php echo esc_html( $c['last_connection']['last_successful_at'] ?: '—' ); ?></td></tr>
			<tr><th>Connector enabled</th><td><?php echo $c['connector_enabled'] ? '<span style="color:green">Yes</span>' : '<span style="color:#d63638">No</span>'; ?></td></tr>
			<tr><th>Read access</th><td><?php echo $c['read_access'] ? 'On' : 'Off'; ?></td></tr>
			<tr><th>Write access</th><td>Off (read-only mirror)</td></tr>
			<tr><th>Plugin version</th><td><?php echo esc_html( $c['plugin_version'] ); ?></td></tr>
			<tr><th>WordPress</th><td><?php echo esc_html( $c['wp_version'] ); ?></td></tr>
			<tr><th>PHP</th><td><?php echo esc_html( $c['php_version'] ); ?></td></tr>
			<tr><th>WooCommerce</th><td><?php echo esc_html( $c['wc_version'] ?? 'not active' ); ?></td></tr>
		</table>
		<?php
		$card( 'Connector', ob_get_clean() );

		// -- Requests card --
		ob_start();
		?>
		<table class="widefat striped" style="width:auto">
			<tr><th>Total requests</th><td><?php echo (int) $r['total']; ?></td></tr>
			<tr><th>Accepted</th><td><?php echo (int) $r['accepted']; ?></td></tr>
			<tr><th>Rejected</th><td><?php echo (int) $r['rejected']; ?></td></tr>
			<tr><th>Last successful</th><td><?php echo esc_html( $r['last_successful_at'] ?: '—' ); ?></td></tr>
			<tr><th>Last failed</th><td><?php echo esc_html( $r['last_failed_at'] ?: '—' ); ?></td></tr>
		</table>
		<?php if ( $reasons_html ) : ?>
		<h3 style="margin-top:16px">Rejection reasons (non-zero)</h3>
		<table class="widefat striped" style="width:auto">
			<thead><tr><th>Reason</th><th>Count</th></tr></thead>
			<tbody><?php echo $reasons_html; // phpcs:ignore ?></tbody>
		</table>
		<?php endif; ?>
		<?php
		$card( 'Requests', ob_get_clean() );

		// -- Exports card --
		ob_start();
		?>
		<table class="widefat striped" style="width:auto">
			<tr><th>Total jobs</th><td><?php echo (int) $e['total_jobs']; ?></td></tr>
			<tr><th>Last created</th><td><?php echo esc_html( $e['last_created_at'] ?: '—' ); ?></td></tr>
			<tr><th>Last downloaded</th><td><?php echo esc_html( $e['last_downloaded_at'] ?: '—' ); ?></td></tr>
			<tr><th>Last job status</th><td><?php echo esc_html( $e['last_status'] ?: '—' ); ?></td></tr>
			<?php if ( $e['last_error'] ) : ?>
			<tr><th>Last error</th><td style="color:#d63638"><?php echo esc_html( $e['last_error'] ); ?></td></tr>
			<?php endif; ?>
		</table>
		<?php
		$card( 'Exports', ob_get_clean() );

		// -- Media card --
		ob_start();
		$counts_by_type = [];
		foreach ( $m['counts'] as $row ) {
			$counts_by_type[ $row['image_type'] ][ $row['status'] ] = (int) $row['cnt'];
		}
		?>
		<table class="widefat striped" style="width:auto">
			<tr><th>Index status</th><td><?php echo esc_html( $m['index_status'] ); ?></td></tr>
			<tr><th>Last full build</th><td><?php echo esc_html( $m['last_full_build_at'] ?: '—' ); ?></td></tr>
		</table>
		<?php if ( $counts_by_type ) : ?>
		<table class="widefat striped" style="width:auto;margin-top:12px">
			<thead><tr><th>Type</th><th>Active</th><th>Deleted</th></tr></thead>
			<tbody>
			<?php foreach ( $counts_by_type as $type => $statuses ) : ?>
				<tr>
					<td><?php echo esc_html( $type ); ?></td>
					<td><?php echo (int) ( $statuses['active'] ?? 0 ); ?></td>
					<td><?php echo (int) ( $statuses['deleted'] ?? 0 ); ?></td>
				</tr>
			<?php endforeach; ?>
			</tbody>
		</table>
		<?php endif; ?>
		<?php
		$card( 'Media', ob_get_clean() );

		// -- Events card --
		ob_start();
		?>
		<table class="widefat striped" style="width:auto">
			<tr><th>Pending</th><td><?php echo (int) ( $v['pending'] ?? 0 ); ?></td></tr>
			<tr><th>Acknowledged</th><td><?php echo (int) ( $v['acknowledged'] ?? 0 ); ?></td></tr>
			<tr><th>Expired</th><td><?php echo (int) ( $v['expired'] ?? 0 ); ?></td></tr>
			<tr><th>Last captured</th><td><?php echo esc_html( $v['last_event_at'] ?: '—' ); ?></td></tr>
			<tr><th>Last acknowledged</th><td><?php echo esc_html( $v['last_ack_at'] ?: '—' ); ?></td></tr>
		</table>
		<?php
		$card( 'Events', ob_get_clean() );

		// -- Cleanup card --
		ob_start();
		if ( empty( $cl ) ) {
			echo '<p>No cleanup run recorded yet (runs hourly via Action Scheduler).</p>';
		} else {
			$s = $cl['last_run_summary'] ?? [];
			?>
			<table class="widefat striped" style="width:auto">
				<tr><th>Last run</th><td><?php echo esc_html( $cl['last_run_at'] ); ?></td></tr>
				<tr><th>Export files cleaned</th><td><?php echo (int) ( $s['export_files_cleaned'] ?? 0 ); ?></td></tr>
				<tr><th>Media rows pruned</th><td><?php echo (int) ( $s['media_rows_pruned'] ?? 0 ); ?></td></tr>
				<tr><th>Event rows pruned</th><td><?php echo (int) ( $s['event_rows_pruned'] ?? 0 ); ?></td></tr>
				<tr><th>Request log rows pruned</th><td><?php echo (int) ( $s['request_log_rows_pruned'] ?? 0 ); ?></td></tr>
			</table>
			<?php
		}
		$card( 'Cleanup', ob_get_clean() );
	}

	// ---------------------------------------------------------------------------
	// Security tab
	// ---------------------------------------------------------------------------

	private static function render_security_tab(): void {
		$settings        = BDSK_Settings::all();
		$using_constant  = BDSK_Security::using_constant();
		$using_encrypted = BDSK_Security::using_encrypted_option();
		$has_secret      = BDSK_Security::has_secret();
		$openssl_ok      = extension_loaded( 'openssl' );
		?>
		<h2>API Key</h2>
		<?php if ( $using_constant ) : ?>
			<p><span style="color:green">&#x2713; API secret is set via the <code>BDSK_API_SECRET</code> constant in wp-config.php.</span></p>
		<?php elseif ( ! $openssl_ok ) : ?>
			<p style="color:#d63638">&#x26A0; <strong>OpenSSL not available.</strong> Enable the PHP OpenSSL extension to use encrypted key storage.</p>
		<?php elseif ( $has_secret ) : ?>
			<p><span style="color:green">&#x2713; An API key is stored (encrypted at rest, AES-256-CBC).</span> Regenerate below if you need a new key — the old key is invalidated immediately.</p>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
			      onsubmit="return confirm('Regenerate the API key? The old key will stop working immediately.');">
				<input type="hidden" name="action" value="bdsk_generate_key" />
				<?php wp_nonce_field( 'bdsk_generate_key' ); ?>
				<?php submit_button( 'Regenerate API Key', 'secondary', 'bdsk_regen', false ); ?>
			</form>
		<?php else : ?>
			<p>No API key configured yet.</p>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
				<input type="hidden" name="action" value="bdsk_generate_key" />
				<?php wp_nonce_field( 'bdsk_generate_key' ); ?>
				<?php submit_button( 'Generate API Key', 'primary', 'bdsk_generate', false ); ?>
			</form>
		<?php endif; ?>

		<hr />

		<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
			<input type="hidden" name="action" value="bdsk_save_security" />
			<?php wp_nonce_field( 'bdsk_save_security' ); ?>

			<h2>Connection</h2>
			<table class="form-table">
				<tr>
					<th scope="row">Connector Enabled</th>
					<td><label><input type="checkbox" name="bdsk_security[enabled]" value="1" <?php checked( $settings['enabled'] ); ?> />
						Master on/off switch for the entire connector.</label></td>
				</tr>
				<tr>
					<th scope="row">Read Access Enabled</th>
					<td><label><input type="checkbox" name="bdsk_security[read_access_enabled]" value="1" <?php checked( $settings['read_access_enabled'] ); ?> />
						Allow Server 2 to call read endpoints.</label></td>
				</tr>
				<tr>
					<th scope="row">Allowed Server IPs</th>
					<td>
						<input type="text" name="bdsk_security[allowed_ips]" value="<?php echo esc_attr( $settings['allowed_ips'] ); ?>" class="regular-text" />
						<p class="description">Comma-separated IPs that may connect (e.g. 1.2.3.4, 5.6.7.8).</p>
					</td>
				</tr>
				<tr>
					<th scope="row">Disable IP Check</th>
					<td><label><input type="checkbox" name="bdsk_security[disable_ip_check]" value="1" <?php checked( $settings['disable_ip_check'] ); ?> />
						<strong style="color:#d63638">WARNING:</strong> disables IP allow-list. Never enable in production.</label></td>
				</tr>
			</table>

			<h2>Rate Limiting</h2>
			<p class="description" style="margin-bottom:12px">Locks out an IP after repeated bad-key attempts. A single successful request resets the counter.</p>
			<table class="form-table">
				<tr>
					<th scope="row">Rate Limit Enabled</th>
					<td><label><input type="checkbox" name="bdsk_security[rate_limit_enabled]" value="1" <?php checked( $settings['rate_limit_enabled'] ?? true ); ?> />
						Enforce per-IP lockout after repeated authentication failures.</label></td>
				</tr>
				<tr>
					<th scope="row">Max Failures Before Lockout</th>
					<td>
						<input type="number" name="bdsk_security[rate_limit_max_failures]" value="<?php echo (int) ( $settings['rate_limit_max_failures'] ?? 10 ); ?>" min="1" max="1000" style="width:80px" />
						<p class="description">Number of consecutive bad-key requests before an IP is rate-limited. Default: 10.</p>
					</td>
				</tr>
				<tr>
					<th scope="row">Lockout Window (minutes)</th>
					<td>
						<input type="number" name="bdsk_security[rate_limit_window_minutes]" value="<?php echo (int) ( $settings['rate_limit_window_minutes'] ?? 15 ); ?>" min="1" max="10080" style="width:80px" />
						<p class="description">How long the lockout lasts after the last failed attempt. Default: 15 minutes.</p>
					</td>
				</tr>
			</table>

			<?php submit_button( 'Save Security Settings' ); ?>
		</form>
		<?php
	}

	// ---------------------------------------------------------------------------
	// Sync Settings tab
	// ---------------------------------------------------------------------------

	private static function render_sync_tab(): void {
		$settings = BDSK_Settings::all();
		$ms       = BDSK_Media_Index::get_status();
		?>
		<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
			<input type="hidden" name="action" value="bdsk_save_sync" />
			<?php wp_nonce_field( 'bdsk_save_sync' ); ?>

			<h2>Export</h2>
			<table class="form-table">
				<tr>
					<th scope="row">Backup Export Enabled</th>
					<td><label><input type="checkbox" name="bdsk_sync[backup_export_enabled]" value="1" <?php checked( $settings['backup_export_enabled'] ); ?> />
						Allow Server 2 to start full DB export jobs.</label></td>
				</tr>
			</table>

			<h2>Media Manifest</h2>
			<table class="form-table">
				<tr>
					<th scope="row">Media Manifest Enabled</th>
					<td><label><input type="checkbox" name="bdsk_sync[media_manifest_enabled]" value="1" <?php checked( $settings['media_manifest_enabled'] ?? true ); ?> />
						Expose <code>/media-manifest</code> endpoint to Server 2.</label></td>
				</tr>
				<tr>
					<th scope="row">Include Evidence Images</th>
					<td><label><input type="checkbox" name="bdsk_sync[include_evidence_images]" value="1" <?php checked( $settings['include_evidence_images'] ?? true ); ?> />
						Index order evidence/receipt images. <strong>WARNING:</strong> may contain personal financial data.</label></td>
				</tr>
				<tr>
					<th scope="row">Index Unknown Media</th>
					<td><label><input type="checkbox" name="bdsk_sync[index_unknown_media]" value="1" <?php checked( $settings['index_unknown_media'] ); ?> />
						Index attachments not linked to any product or order (avoids theme/logo clutter — default OFF).</label></td>
				</tr>
				<tr>
					<th scope="row">Evidence Image Meta Keys</th>
					<td>
						<input type="text" name="bdsk_sync[evidence_meta_keys]" value="<?php echo esc_attr( $settings['evidence_meta_keys'] ); ?>" class="regular-text" />
						<p class="description">Comma-separated order meta keys that hold attachment IDs for evidence images.</p>
					</td>
				</tr>
			</table>

			<h2>Event Sync</h2>
			<table class="form-table">
				<tr>
					<th scope="row">Event Sync Enabled</th>
					<td><label><input type="checkbox" name="bdsk_sync[event_sync_enabled]" value="1" <?php checked( $settings['event_sync_enabled'] ?? true ); ?> />
						Expose <code>/events/*</code> and <code>/snapshot/*</code> endpoints and capture change events.</label></td>
				</tr>
			</table>

			<h2>Development</h2>
			<table class="form-table">
				<tr>
					<th scope="row">Enable Debug Log</th>
					<td><label><input type="checkbox" name="bdsk_sync[debug_log_enabled]" value="1" <?php checked( $settings['debug_log_enabled'] ); ?> />
						In <code>local_private_archive_mode</code>: writes to <code>wp-content/bdsk-debug.log</code>. Otherwise routes to PHP <code>error_log()</code>. <strong>Never enable in production.</strong></label></td>
				</tr>
			</table>

			<?php submit_button( 'Save Sync Settings' ); ?>
		</form>

		<hr />

		<h2>Media Index</h2>
		<?php
		$ms_label = match( $ms['status'] ) {
			'running' => '<span style="color:orange">Running — step: ' . esc_html( $ms['current_step'] ?? '' ) . ', offset: ' . (int) ( $ms['current_offset'] ?? 0 ) . '</span>',
			'idle'    => '<span style="color:green">Idle</span>',
			default   => esc_html( $ms['status'] ),
		};
		?>
		<table class="widefat striped" style="width:auto">
			<tr><th>Index Status</th><td><?php echo wp_kses( $ms_label, [ 'span' => [ 'style' => [] ] ] ); ?></td></tr>
			<tr><th>Last Full Build</th><td><?php echo esc_html( $ms['last_full_build_at'] ?: '—' ); ?></td></tr>
			<?php if ( $ms['last_error'] ?? '' ) : ?>
			<tr><th>Last Error</th><td style="color:#d63638"><?php echo esc_html( $ms['last_error'] ); ?></td></tr>
			<?php endif; ?>
		</table>
		<?php if ( 'running' !== $ms['status'] ) : ?>
		<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="margin-top:12px">
			<input type="hidden" name="action" value="bdsk_rebuild_media_index" />
			<?php wp_nonce_field( 'bdsk_rebuild_media_index' ); ?>
			<?php submit_button( 'Rebuild Media Index Now', 'secondary', 'bdsk_rebuild_media', false ); ?>
		</form>
		<?php else : ?>
		<p style="margin-top:12px"><em>Build in progress — refresh to check status.</em></p>
		<?php endif; ?>
		<?php
	}

	// ---------------------------------------------------------------------------
	// Job History tab
	// ---------------------------------------------------------------------------

	private static function render_history_tab(): void {
		global $wpdb;

		$jobs = $wpdb->get_results(
			"SELECT job_id, status, created_at, started_at, finished_at,
			        archive_size, exported_rows_count, last_error
			 FROM " . BDSK_DB::jobs_table() . "
			 ORDER BY id DESC LIMIT 20",
			ARRAY_A
		) ?: [];
		?>
		<h2>Recent Export Jobs (last 20)</h2>
		<?php if ( empty( $jobs ) ) : ?>
			<p>No export jobs yet.</p>
		<?php else : ?>
		<table class="widefat striped">
			<thead>
				<tr>
					<th>Job ID</th>
					<th>Status</th>
					<th>Created</th>
					<th>Finished</th>
					<th>Duration</th>
					<th>Archive Size</th>
					<th>Rows Exported</th>
					<th>Error</th>
				</tr>
			</thead>
			<tbody>
			<?php foreach ( $jobs as $job ) : ?>
				<?php
				$short_id = substr( $job['job_id'], 0, 8 );
				$duration = '—';
				if ( $job['started_at'] && $job['finished_at'] ) {
					$secs     = max( 0, strtotime( $job['finished_at'] ) - strtotime( $job['started_at'] ) );
					$duration = $secs < 60
						? "{$secs}s"
						: floor( $secs / 60 ) . 'm ' . ( $secs % 60 ) . 's';
				}
				$size     = $job['archive_size']
					? self::format_bytes( (int) $job['archive_size'] )
					: '—';
				$error_short = $job['last_error']
					? substr( $job['last_error'], 0, 80 ) . ( strlen( $job['last_error'] ) > 80 ? '…' : '' )
					: '';
				$status_color = match( $job['status'] ) {
					'ready', 'downloaded', 'cleaned' => 'color:green',
					'failed', 'expired'               => 'color:#d63638',
					'running'                         => 'color:orange',
					default                           => '',
				};
				?>
				<tr>
					<td><code title="<?php echo esc_attr( $job['job_id'] ); ?>"><?php echo esc_html( $short_id ); ?>…</code></td>
					<td><span style="<?php echo esc_attr( $status_color ); ?>"><?php echo esc_html( $job['status'] ); ?></span></td>
					<td><?php echo esc_html( $job['created_at'] ); ?></td>
					<td><?php echo esc_html( $job['finished_at'] ?: '—' ); ?></td>
					<td><?php echo esc_html( $duration ); ?></td>
					<td><?php echo esc_html( $size ); ?></td>
					<td><?php echo esc_html( $job['exported_rows_count'] ?? '—' ); ?></td>
					<td>
						<?php if ( $error_short ) : ?>
						<span title="<?php echo esc_attr( $job['last_error'] ); ?>" style="color:#d63638">
							<?php echo esc_html( $error_short ); ?>
						</span>
						<?php endif; ?>
					</td>
				</tr>
			<?php endforeach; ?>
			</tbody>
		</table>
		<?php endif; ?>
		<?php
	}

	private static function format_bytes( int $bytes ): string {
		if ( $bytes < 1024 ) {
			return "{$bytes} B";
		}
		if ( $bytes < 1048576 ) {
			return round( $bytes / 1024, 1 ) . ' KB';
		}
		if ( $bytes < 1073741824 ) {
			return round( $bytes / 1048576, 1 ) . ' MB';
		}
		return round( $bytes / 1073741824, 2 ) . ' GB';
	}

	// ---------------------------------------------------------------------------
	// Danger Zone tab
	// ---------------------------------------------------------------------------

	private static function render_danger_tab(): void {
		?>
		<h2>Emergency Cleanup</h2>
		<p>Deletes all export archive files from disk, marks all active jobs as failed, and cancels queued export tasks. Use this if something went wrong and you need a clean slate.</p>
		<p>Does <strong>not</strong> affect <code>bdsk_media_index</code>, <code>bdsk_event_outbox</code>, or request statistics.</p>
		<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
		      onsubmit="return confirm('Delete all export archives and reset all job state? This cannot be undone.');">
			<input type="hidden" name="action" value="bdsk_emergency_cleanup" />
			<?php wp_nonce_field( 'bdsk_emergency_cleanup' ); ?>
			<?php submit_button( 'Run Emergency Cleanup', 'delete', 'bdsk_emergency', false ); ?>
		</form>

		<hr />

		<h2>Reset Counters</h2>
		<p>Resets all-time request statistics (<code>bdsk_stats_totals</code>) to zero <strong>and</strong> truncates the recent request log (<code>bdsk_request_log</code>). Cannot be undone.</p>
		<p>Does <strong>not</strong> affect <code>bdsk_export_jobs</code>, <code>bdsk_media_index</code>, or <code>bdsk_event_outbox</code>.</p>
		<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
		      onsubmit="return confirm('Reset ALL request statistics and clear the request log? This cannot be undone.');">
			<input type="hidden" name="action" value="bdsk_reset_counters" />
			<label style="display:block;margin-bottom:12px">
				<input type="checkbox" name="bdsk_confirm_reset" value="1" required
				       onclick="this.form.querySelector('[type=submit]').disabled = !this.checked;" />
				I understand this clears all-time statistics and recent request history.
			</label>
			<?php wp_nonce_field( 'bdsk_reset_counters' ); ?>
			<?php submit_button( 'Reset Counters', 'delete', 'bdsk_reset', false, [ 'disabled' => 'disabled' ] ); ?>
		</form>
		<?php
	}

	// ---------------------------------------------------------------------------
	// Form handlers
	// ---------------------------------------------------------------------------

	public static function handle_save_security(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised.' );
		}
		check_admin_referer( 'bdsk_save_security' );

		$input    = $_POST['bdsk_security'] ?? []; // phpcs:ignore WordPress.Security
		$existing = BDSK_Settings::all();

		BDSK_Settings::bulk_update( array_merge( $existing, [
			'enabled'                   => ! empty( $input['enabled'] ),
			'read_access_enabled'       => ! empty( $input['read_access_enabled'] ),
			'allowed_ips'               => sanitize_text_field( $input['allowed_ips'] ?? '' ),
			'disable_ip_check'          => ! empty( $input['disable_ip_check'] ),
			'rate_limit_enabled'        => ! empty( $input['rate_limit_enabled'] ),
			'rate_limit_max_failures'   => max( 1, (int) ( $input['rate_limit_max_failures'] ?? 10 ) ),
			'rate_limit_window_minutes' => max( 1, (int) ( $input['rate_limit_window_minutes'] ?? 15 ) ),
		] ) );

		wp_safe_redirect( admin_url( 'options-general.php?page=' . self::PAGE_SLUG . '&tab=security&bdsk_notice=saved' ) );
		exit;
	}

	public static function handle_save_sync(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised.' );
		}
		check_admin_referer( 'bdsk_save_sync' );

		$input    = $_POST['bdsk_sync'] ?? []; // phpcs:ignore WordPress.Security
		$existing = BDSK_Settings::all();

		BDSK_Settings::bulk_update( array_merge( $existing, [
			'backup_export_enabled'  => ! empty( $input['backup_export_enabled'] ),
			'media_manifest_enabled' => ! empty( $input['media_manifest_enabled'] ),
			'include_evidence_images' => ! empty( $input['include_evidence_images'] ),
			'index_unknown_media'    => ! empty( $input['index_unknown_media'] ),
			'evidence_meta_keys'     => sanitize_text_field( $input['evidence_meta_keys'] ?? '' ),
			'event_sync_enabled'     => ! empty( $input['event_sync_enabled'] ),
			'debug_log_enabled'      => ! empty( $input['debug_log_enabled'] ),
		] ) );

		wp_safe_redirect( admin_url( 'options-general.php?page=' . self::PAGE_SLUG . '&tab=sync&bdsk_notice=saved' ) );
		exit;
	}

	public static function handle_generate_key(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised.' );
		}
		check_admin_referer( 'bdsk_generate_key' );

		$is_regen  = BDSK_Security::has_secret() && ! BDSK_Security::using_constant();
		$plaintext = BDSK_Security::generate_and_store();
		set_transient( 'bdsk_flash_new_key', $plaintext, 300 );

		if ( $is_regen ) {
			BDSK_DB::invalidate_all_download_tokens();
		}

		wp_safe_redirect( admin_url( 'options-general.php?page=' . self::PAGE_SLUG . '&tab=security' ) );
		exit;
	}

	public static function handle_rebuild_media_index(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised.' );
		}
		check_admin_referer( 'bdsk_rebuild_media_index' );

		$result = BDSK_Media_Index::schedule_full_build();
		$notice = is_wp_error( $result ) ? 'media_rebuild_error' : 'media_rebuild_started';

		wp_safe_redirect( admin_url( 'options-general.php?page=' . self::PAGE_SLUG . '&tab=sync&bdsk_notice=' . $notice ) );
		exit;
	}

	public static function handle_reset_counters(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised.' );
		}
		check_admin_referer( 'bdsk_reset_counters' );

		if ( empty( $_POST['bdsk_confirm_reset'] ) ) {
			wp_safe_redirect( admin_url( 'options-general.php?page=' . self::PAGE_SLUG . '&tab=danger' ) );
			exit;
		}

		global $wpdb;

		BDSK_Stats::reset_totals();
		$wpdb->query( 'TRUNCATE TABLE ' . BDSK_DB::log_table() );

		bdsk_log( 'Counters reset and request log truncated by admin.' );

		wp_safe_redirect( admin_url( 'options-general.php?page=' . self::PAGE_SLUG . '&tab=danger&bdsk_notice=counters_reset' ) );
		exit;
	}
}
