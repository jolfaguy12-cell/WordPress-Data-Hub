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
		add_action( 'admin_notices', [ __CLASS__, 'maybe_show_notice' ] );
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

		add_settings_section(
			'bdsk_main',
			'Connection Settings',
			'__return_false',
			self::PAGE_SLUG
		);

		add_settings_section(
			'bdsk_security',
			'Security',
			'__return_false',
			self::PAGE_SLUG
		);

		add_settings_section(
			'bdsk_debug',
			'Development',
			'__return_false',
			self::PAGE_SLUG
		);

		$fields = [
			// [ id, label, section, type, description ]
			[ 'enabled',               'Connector Enabled',             'bdsk_main',     'checkbox', 'Master on/off switch for the entire connector.' ],
			[ 'read_access_enabled',   'Read Access Enabled',           'bdsk_main',     'checkbox', 'Allow Server 2 to call read endpoints.' ],
			[ 'backup_export_enabled', 'Backup Export Enabled',         'bdsk_main',     'checkbox', 'Allow Server 2 to start DB export jobs.' ],
			[ 'allowed_ips',           'Allowed Server IPs',            'bdsk_security', 'text',     'Comma-separated list of IPs that may connect (e.g. 1.2.3.4, 5.6.7.8).' ],
			[ 'disable_ip_check',      'Disable IP Check (dev only)',   'bdsk_security', 'checkbox', 'WARNING: disables IP allow-list. Never enable this in production.' ],
			[ 'api_key_hash',          'API Key (fallback)',            'bdsk_security', 'password', 'Used only when <code>BDSK_API_SECRET</code> is not defined in wp-config.php. Setting the constant is more secure.' ],
			[ 'debug_log_enabled',     'Enable Debug Log',              'bdsk_debug',    'checkbox', 'Writes to <code>wp-content/bdsk-debug.log</code>. Never enable in production.' ],
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
		} elseif ( 'password' === $type ) {
			// API key fallback — only show if constant not defined
			if ( BDSK_Security::using_constant() ) {
				echo '<p><em>API secret is set via the <code>BDSK_API_SECRET</code> constant in wp-config.php (recommended). The field below is ignored.</em></p>';
			} else {
				printf(
					'<input type="password" name="%s[%s]" value="%s" class="regular-text" autocomplete="new-password" /><p class="description">%s</p>',
					esc_attr( self::OPTION_KEY ),
					esc_attr( $id ),
					'', // never echo the hash back
					wp_kses( $desc, [ 'code' => [] ] )
				);
				echo '<p class="description" style="color:#d63638"><strong>Recommendation:</strong> Define <code>BDSK_API_SECRET</code> in wp-config.php instead of using this field.</p>';
			}
			return;
		} else {
			printf(
				'<input type="text" name="%s[%s]" value="%s" class="regular-text" />',
				esc_attr( self::OPTION_KEY ),
				esc_attr( $id ),
				esc_attr( (string) $value )
			);
		}

		if ( $desc && 'checkbox' !== $type ) {
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

		$clean = [
			'enabled'               => ! empty( $input['enabled'] ),
			'read_access_enabled'   => ! empty( $input['read_access_enabled'] ),
			'backup_export_enabled' => ! empty( $input['backup_export_enabled'] ),
			'allowed_ips'           => sanitize_text_field( $input['allowed_ips'] ?? '' ),
			'disable_ip_check'      => ! empty( $input['disable_ip_check'] ),
			'debug_log_enabled'     => ! empty( $input['debug_log_enabled'] ),
			// Preserve stats fields
			'last_successful_request' => $existing['last_successful_request'],
			'last_failed_request'     => $existing['last_failed_request'],
			'last_export_downloaded'  => $existing['last_export_downloaded'],
		];

		// API key fallback — only store hash, never plaintext
		if ( ! BDSK_Security::using_constant() && ! empty( $input['api_key_hash'] ) ) {
			$clean['api_key_hash'] = hash( 'sha256', $input['api_key_hash'] );
		} else {
			$clean['api_key_hash'] = $existing['api_key_hash'];
		}

		return $clean;
	}

	// ---------------------------------------------------------------------------
	// Admin notices
	// ---------------------------------------------------------------------------

	public static function maybe_show_notice(): void {
		$screen = get_current_screen();
		if ( ! $screen || 'settings_page_' . self::PAGE_SLUG !== $screen->id ) {
			return;
		}

		// IP check disabled warning
		if ( BDSK_Settings::get( 'disable_ip_check' ) ) {
			echo '<div class="notice notice-warning"><p><strong>Behdashtik Mirror Connector:</strong> IP check is disabled. This should only be used during local development.</p></div>';
		}

		// Using hash-stored key warning
		if ( ! BDSK_Security::using_constant() && '' !== BDSK_Settings::get( 'api_key_hash' ) ) {
			echo '<div class="notice notice-info"><p><strong>Behdashtik Mirror Connector:</strong> You are using a stored key hash. For better security, define <code>BDSK_API_SECRET</code> in wp-config.php and leave the key field blank.</p></div>';
		}

		// Cleanup confirmation
		// phpcs:ignore WordPress.Security.NonceVerification.Recommended
		if ( isset( $_GET['bdsk_notice'] ) && 'cleanup_done' === $_GET['bdsk_notice'] ) {
			echo '<div class="notice notice-success is-dismissible"><p><strong>Behdashtik Mirror Connector:</strong> Emergency cleanup completed.</p></div>';
		}
	}

	// ---------------------------------------------------------------------------
	// Render
	// ---------------------------------------------------------------------------

	public static function render(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( 'Unauthorised.' );
		}

		$settings = BDSK_Settings::all();
		?>
		<div class="wrap">
			<h1>Behdashtik Mirror Connector</h1>

			<form method="post" action="options.php">
				<?php
				settings_fields( 'bdsk_settings_group' );
				do_settings_sections( self::PAGE_SLUG );
				submit_button( 'Save Settings' );
				?>
			</form>

			<hr />

			<h2>Status</h2>
			<table class="form-table">
				<tr>
					<th>Plugin Version</th>
					<td><?php echo esc_html( BDSK_VERSION ); ?></td>
				</tr>
				<tr>
					<th>API Secret Source</th>
					<td><?php echo BDSK_Security::using_constant() ? '<span style="color:green">✓ wp-config.php constant</span>' : '<span style="color:orange">⚠ Stored hash (fallback)</span>'; ?></td>
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

			<h2>Emergency Cleanup</h2>
			<p>Deletes all export archive files from disk, marks all pending/running jobs as failed, and clears all cleanup queues. Use this if something went wrong and you need a clean slate.</p>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
			      onsubmit="return confirm('Delete all export archives and reset job state?');">
				<input type="hidden" name="action" value="bdsk_emergency_cleanup" />
				<?php wp_nonce_field( 'bdsk_emergency_cleanup' ); ?>
				<?php submit_button( 'Run Emergency Cleanup', 'delete', 'bdsk_emergency', false ); ?>
			</form>
		</div>
		<?php
	}
}
