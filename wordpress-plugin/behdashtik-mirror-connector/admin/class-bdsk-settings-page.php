<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class BDSK_Settings_Page {

	private const PAGE_SLUG  = 'bdsk-settings';
	private const OPTION_KEY = 'bdsk_settings';

	public static function init(): void {
		add_action( 'admin_menu',    [ __CLASS__, 'add_menu' ] );
		add_action( 'admin_init',    [ __CLASS__, 'register_settings' ] );
		add_action( 'admin_init',    [ __CLASS__, 'maybe_redirect_after_activation' ] );
		add_action( 'admin_notices', [ __CLASS__, 'maybe_show_notice' ] );
		add_action( 'admin_post_bdsk_generate_key',       [ __CLASS__, 'handle_generate_key' ] );
		add_action( 'admin_post_bdsk_emergency_cleanup',  [ __CLASS__, 'handle_emergency_cleanup' ] );
		add_action( 'admin_post_bdsk_rebuild_media_index', [ __CLASS__, 'handle_rebuild_media_index' ] );
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
	// Settings API registration
	// ---------------------------------------------------------------------------

	public static function register_settings(): void {
		register_setting(
			'bdsk_settings_group',
			self::OPTION_KEY,
			[
				'sanitize_callback' => [ __CLASS__, 'sanitize' ],
			]
		);

		add_settings_section( 'bdsk_main',     'Connection Settings', '__return_false', self::PAGE_SLUG );
		add_settings_section( 'bdsk_security', 'Security',            '__return_false', self::PAGE_SLUG );
		add_settings_section( 'bdsk_media',    'Media Manifest',      '__return_false', self::PAGE_SLUG );
		add_settings_section( 'bdsk_debug',    'Development',         '__return_false', self::PAGE_SLUG );

		$fields = [
			// [ id, label, section, type, description ]
			[ 'enabled',                 'Connector Enabled',              'bdsk_main',     'checkbox', 'Master on/off switch for the entire connector.' ],
			[ 'read_access_enabled',     'Read Access Enabled',            'bdsk_main',     'checkbox', 'Allow Server 2 to call read endpoints.' ],
			[ 'backup_export_enabled',   'Backup Export Enabled',          'bdsk_main',     'checkbox', 'Allow Server 2 to start DB export jobs.' ],
			[ 'allowed_ips',             'Allowed Server IPs',             'bdsk_security', 'text',     'Comma-separated IPs that may connect (e.g. 1.2.3.4, 5.6.7.8).' ],
			[ 'disable_ip_check',        'Disable IP Check (dev only)',    'bdsk_security', 'checkbox', 'WARNING: disables IP allow-list. Never enable in production.' ],
			[ 'media_manifest_enabled',  'Media Manifest Enabled',         'bdsk_media',    'checkbox', 'Expose the <code>/media-manifest</code> endpoint to Server 2.' ],
			[ 'include_evidence_images', 'Include Evidence Images',        'bdsk_media',    'checkbox', 'Index order evidence/receipt images. WARNING: may contain personal financial data — protect Server 2 storage accordingly.' ],
			[ 'index_unknown_media',     'Index Unknown Media',            'bdsk_media',    'checkbox', 'Index attachments not linked to any product or order. Default OFF (avoids theme/logo clutter).' ],
			[ 'evidence_meta_keys',      'Evidence Image Meta Keys',       'bdsk_media',    'text',     'Comma-separated order meta keys that hold WP attachment IDs for evidence images. Leave empty if not used.' ],
			[ 'debug_log_enabled',       'Enable Debug Log',               'bdsk_debug',    'checkbox', 'Writes to <code>wp-content/bdsk-debug.log</code>. Never enable in production.' ],
		];

		foreach ( $fields as [ $id, $label, $section, $type, $desc ] ) {
			add_settings_field(
				'bdsk_' . $id,
				$label,
				[ __CLASS__, 'render_field' ],
				self::PAGE_SLUG,
				$section,
				[ 'id' => $id, 'type' => $type, 'description' => $desc ]
			);
		}
	}

	public static function render_field( array $args ): void {
		$id       = $args['id'];
		$type     = $args['type'];
		$desc     = $args['description'];
		$settings = BDSK_Settings::all();
		$value    = $settings[ $id ] ?? '';

		if ( 'checkbox' === $type ) {
			printf(
				'<label><input type="checkbox" name="%s[%s]" value="1" %s /> %s</label>',
				esc_attr( self::OPTION_KEY ),
				esc_attr( $id ),
				checked( $value, true, false ),
				wp_kses( $desc, [ 'code' => [] ] )
			);
		} else {
			printf(
				'<input type="text" name="%s[%s]" value="%s" class="regular-text" />',
				esc_attr( self::OPTION_KEY ),
				esc_attr( $id ),
				esc_attr( (string) $value )
			);
			echo '<p class="description">' . wp_kses( $desc, [ 'code' => [] ] ) . '</p>';
		}
	}

	// ---------------------------------------------------------------------------
	// Sanitize callback
	// ---------------------------------------------------------------------------

	public static function sanitize( mixed $input ): array {
		if ( ! is_array( $input ) ) {
			return BDSK_Settings::all();
		}

		$existing = BDSK_Settings::all();

		return [
			'enabled'                => ! empty( $input['enabled'] ),
			'read_access_enabled'    => ! empty( $input['read_access_enabled'] ),
			'backup_export_enabled'  => ! empty( $input['backup_export_enabled'] ),
			'allowed_ips'            => sanitize_text_field( $input['allowed_ips'] ?? '' ),
			'disable_ip_check'       => ! empty( $input['disable_ip_check'] ),
			'media_manifest_enabled' => ! empty( $input['media_manifest_enabled'] ),
			'include_evidence_images' => ! empty( $input['include_evidence_images'] ),
			'index_unknown_media'    => ! empty( $input['index_unknown_media'] ),
			'evidence_meta_keys'     => sanitize_text_field( $input['evidence_meta_keys'] ?? '' ),
			'debug_log_enabled'      => ! empty( $input['debug_log_enabled'] ),
			// Legacy hash preserved — not written by the new generate flow
			'api_key_hash'            => $existing['api_key_hash'],
			// Preserve stats fields
			'last_successful_request' => $existing['last_successful_request'],
			'last_failed_request'     => $existing['last_failed_request'],
			'last_export_downloaded'  => $existing['last_export_downloaded'],
		];
	}

	// ---------------------------------------------------------------------------
	// Generate / Regenerate key handler
	// ---------------------------------------------------------------------------

	public static function handle_generate_key(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised.' );
		}
		check_admin_referer( 'bdsk_generate_key' );

		$is_regen = BDSK_Security::has_secret() && ! BDSK_Security::using_constant();

		$plaintext = BDSK_Security::generate_and_store();

		// Store the plaintext for one-time display on the redirect target
		set_transient( 'bdsk_flash_new_key', $plaintext, 300 );

		// Invalidate any in-flight download tokens so they fail with the old key
		if ( $is_regen ) {
			BDSK_DB::invalidate_all_download_tokens();
		}

		wp_safe_redirect( admin_url( 'options-general.php?page=' . self::PAGE_SLUG ) );
		exit;
	}

	// ---------------------------------------------------------------------------
	// Rebuild media index handler
	// ---------------------------------------------------------------------------

	public static function handle_rebuild_media_index(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised.' );
		}
		check_admin_referer( 'bdsk_rebuild_media_index' );

		$result = BDSK_Media_Index::schedule_full_build();
		$notice = is_wp_error( $result ) ? 'media_rebuild_error' : 'media_rebuild_started';

		wp_safe_redirect( admin_url( 'options-general.php?page=' . self::PAGE_SLUG . '&bdsk_notice=' . $notice ) );
		exit;
	}

	// ---------------------------------------------------------------------------
	// Emergency cleanup handler
	// ---------------------------------------------------------------------------

	public static function handle_emergency_cleanup(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised.' );
		}
		check_admin_referer( 'bdsk_emergency_cleanup' );

		global $wpdb;

		// Mark all active jobs as failed
		$wpdb->query(
			"UPDATE " . BDSK_DB::jobs_table() . "
			 SET status = 'failed', last_error = 'Emergency cleanup by admin'
			 WHERE status IN ('pending','running','ready','downloading')"
		);

		// Delete all export archive directories
		$base = BDSK_Export_Job::get_export_base();
		if ( is_dir( $base ) ) {
			$dirs = glob( $base . '/*', GLOB_ONLYDIR );
			if ( $dirs ) {
				foreach ( $dirs as $dir ) {
					// phpcs:ignore WordPress.PHP.NoSilencedErrors.Discouraged
					array_map( 'unlink', glob( $dir . '/*' ) ?: [] );
					@rmdir( $dir );
				}
			}
		}

		// Cancel all queued AS actions for this plugin
		if ( function_exists( 'as_unschedule_all_actions' ) ) {
			as_unschedule_all_actions( 'bdsk_export_chunk' );
		}

		bdsk_log( 'Emergency cleanup executed by admin.' );

		wp_safe_redirect(
			admin_url( 'options-general.php?page=' . self::PAGE_SLUG . '&bdsk_notice=cleanup_done' )
		);
		exit;
	}

	// ---------------------------------------------------------------------------
	// Admin notices
	// ---------------------------------------------------------------------------

	public static function maybe_show_notice(): void {
		// Warn if IP check is disabled — visible on ALL admin pages so it's hard to miss
		if ( BDSK_Settings::get( 'disable_ip_check' ) && BDSK_Settings::get( 'enabled' ) ) {
			echo '<div class="notice notice-warning"><p><strong>Behdashtik Mirror Connector:</strong> IP check is disabled. Only use this in local development, never in production.</p></div>';
		}

		$screen = get_current_screen();
		if ( ! $screen || 'settings_page_' . self::PAGE_SLUG !== $screen->id ) {
			return;
		}

		// phpcs:ignore WordPress.Security.NonceVerification.Recommended
		// phpcs:ignore WordPress.Security.NonceVerification.Recommended
		switch ( $_GET['bdsk_notice'] ?? '' ) {
			case 'cleanup_done':
				echo '<div class="notice notice-success is-dismissible"><p><strong>Behdashtik Mirror Connector:</strong> Emergency cleanup completed.</p></div>';
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
	// Render
	// ---------------------------------------------------------------------------

	public static function render(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised.' );
		}

		$settings         = BDSK_Settings::all();
		$using_constant   = BDSK_Security::using_constant();
		$using_encrypted  = BDSK_Security::using_encrypted_option();
		$has_secret       = BDSK_Security::has_secret();
		$openssl_ok       = extension_loaded( 'openssl' );

		// ---- Show-once new-key flash ----
		$flash_key = get_transient( 'bdsk_flash_new_key' );
		if ( $flash_key ) {
			delete_transient( 'bdsk_flash_new_key' ); // shown exactly once
			?>
			<div class="notice notice-warning" style="padding:16px">
				<p><strong>&#x26A0; Save this API key now — it will not be shown again.</strong></p>
				<p>Copy it into your <code>config.json</code> on Server 2 as the <code>api_secret</code> value.</p>
				<div style="display:flex;gap:8px;align-items:center;margin-top:8px">
					<input
						type="text"
						id="bdsk-new-key"
						value="<?php echo esc_attr( $flash_key ); ?>"
						readonly
						style="font-family:monospace;width:520px;font-size:13px"
					/>
					<button
						type="button"
						onclick="navigator.clipboard.writeText(document.getElementById('bdsk-new-key').value).then(function(){this.textContent='Copied!';}.bind(this))"
						class="button"
					>Copy</button>
				</div>
			</div>
			<?php
		}
		?>
		<div class="wrap">
			<h1>Behdashtik Mirror Connector</h1>

			<?php if ( ! $has_secret && ! $using_constant ) : ?>
			<div class="notice notice-error">
				<p><strong>No API key configured.</strong> Generate one below to allow Server 2 to connect.</p>
			</div>
			<?php endif; ?>

			<?php if ( $using_encrypted && ! extension_loaded( 'openssl' ) ) : ?>
			<div class="notice notice-error">
				<p><strong>OpenSSL extension not available.</strong> The stored API key cannot be decrypted. Enable <code>extension=openssl</code> in php.ini and reload.</p>
			</div>
			<?php endif; ?>

			<form method="post" action="options.php">
				<?php
				settings_fields( 'bdsk_settings_group' );
				do_settings_sections( self::PAGE_SLUG );
				submit_button( 'Save Settings' );
				?>
			</form>

			<hr />

			<h2>API Key</h2>

			<?php if ( $using_constant ) : ?>
			<p><span style="color:green">&#x2713; API secret is set via the <code>BDSK_API_SECRET</code> constant in wp-config.php.</span> Key generation is disabled while the constant is defined.</p>

			<?php elseif ( ! $openssl_ok ) : ?>
			<p style="color:#d63638">&#x26A0; <strong>OpenSSL not available.</strong> Encrypted key storage requires the PHP OpenSSL extension. Enable it in php.ini to use this feature.</p>

			<?php else : ?>
				<?php if ( $has_secret ) : ?>
				<p>
					<span style="color:green">&#x2713; An API key is stored (encrypted at rest with AES-256-CBC).</span>
					If you have lost the key, regenerate it below — the old key will be invalidated immediately.
				</p>
				<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
				      onsubmit="return confirm('Regenerate the API key? The old key will stop working immediately.');">
					<input type="hidden" name="action" value="bdsk_generate_key" />
					<?php wp_nonce_field( 'bdsk_generate_key' ); ?>
					<?php submit_button( 'Regenerate API Key', 'secondary', 'bdsk_regen', false ); ?>
				</form>

				<?php if ( '' !== BDSK_Settings::get( 'api_key_hash', '' ) ) : ?>
				<details style="margin-top:12px">
					<summary style="cursor:pointer;color:#646970">Legacy hash field (no longer needed)</summary>
					<p class="description" style="margin-top:8px">
						Your previous install used a manually-entered key stored as a SHA-256 hash.
						That hash is preserved for backward compatibility but the new encrypted key takes priority.
						You can clear it by saving the settings form once (it will be retained as-is but the encrypted key will be used for authentication).
					</p>
				</details>
				<?php endif; ?>

				<?php else : ?>
				<p>No API key has been configured yet. Click below to generate one.</p>
				<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
					<input type="hidden" name="action" value="bdsk_generate_key" />
					<?php wp_nonce_field( 'bdsk_generate_key' ); ?>
					<?php submit_button( 'Generate API Key', 'primary', 'bdsk_generate', false ); ?>
				</form>
				<?php endif; ?>

			<?php endif; ?>

			<hr />

			<h2>Status</h2>
			<table class="form-table widefat" style="width:auto">
				<tr>
					<th>Plugin Version</th>
					<td><?php echo esc_html( BDSK_VERSION ); ?></td>
				</tr>
				<tr>
					<th>API Secret Source</th>
					<td>
						<?php
						if ( $using_constant ) {
							echo '<span style="color:green">wp-config.php constant</span>';
						} elseif ( $using_encrypted ) {
							echo '<span style="color:green">Encrypted option (AES-256-CBC)</span>';
						} elseif ( '' !== BDSK_Settings::get( 'api_key_hash', '' ) ) {
							echo '<span style="color:orange">Legacy hash (regenerate recommended)</span>';
						} else {
							echo '<span style="color:#d63638">Not configured</span>';
						}
						?>
					</td>
				</tr>
				<tr>
					<th>OpenSSL Available</th>
					<td><?php echo $openssl_ok ? '<span style="color:green">Yes</span>' : '<span style="color:#d63638">No — key storage disabled</span>'; ?></td>
				</tr>
				<tr>
					<th>Last Successful Request</th>
					<td><?php echo esc_html( $settings['last_successful_request'] ?: '—' ); ?></td>
				</tr>
				<tr>
					<th>Last Failed Request</th>
					<td><?php echo esc_html( $settings['last_failed_request'] ?: '—' ); ?></td>
				</tr>
				<tr>
					<th>Last Export Downloaded</th>
					<td><?php echo esc_html( $settings['last_export_downloaded'] ?: '—' ); ?></td>
				</tr>
			</table>

			<hr />

			<h2>Media Index</h2>

			<?php
			$media_status = BDSK_Media_Index::get_status();
			$ms_label     = match( $media_status['status'] ) {
				'running' => '<span style="color:orange">Running — step: ' . esc_html( $media_status['current_step'] ) . ', offset: ' . (int) $media_status['current_offset'] . '</span>',
				'idle'    => '<span style="color:green">Idle</span>',
				default   => esc_html( $media_status['status'] ),
			};
			?>
			<table class="form-table widefat" style="width:auto">
				<tr>
					<th>Index Status</th>
					<td><?php echo wp_kses( $ms_label, [ 'span' => [ 'style' => [] ] ] ); ?></td>
				</tr>
				<tr>
					<th>Last Full Build</th>
					<td><?php echo esc_html( $media_status['last_full_build_at'] ?: '—' ); ?></td>
				</tr>
				<?php if ( $media_status['last_error'] ) : ?>
				<tr>
					<th>Last Error</th>
					<td style="color:#d63638"><?php echo esc_html( $media_status['last_error'] ); ?></td>
				</tr>
				<?php endif; ?>
			</table>

			<?php if ( 'running' !== $media_status['status'] ) : ?>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="margin-top:12px">
				<input type="hidden" name="action" value="bdsk_rebuild_media_index" />
				<?php wp_nonce_field( 'bdsk_rebuild_media_index' ); ?>
				<?php submit_button( 'Rebuild Media Index Now', 'secondary', 'bdsk_rebuild_media', false ); ?>
			</form>
			<?php else : ?>
			<p style="margin-top:12px"><em>Build in progress — refresh to check status.</em></p>
			<?php endif; ?>

			<hr />

			<h2>Emergency Cleanup</h2>
			<p>Deletes all export archive files from disk, marks all active jobs as failed, and cancels queued export tasks. Use this if something went wrong and you need a clean slate.</p>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
			      onsubmit="return confirm('Delete all export archives and reset all job state?');">
				<input type="hidden" name="action" value="bdsk_emergency_cleanup" />
				<?php wp_nonce_field( 'bdsk_emergency_cleanup' ); ?>
				<?php submit_button( 'Run Emergency Cleanup', 'delete', 'bdsk_emergency', false ); ?>
			</form>
		</div>
		<?php
	}
}
