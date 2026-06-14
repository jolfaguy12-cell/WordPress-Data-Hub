<?php
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

// ---------------------------------------------------------------------------
// Settings helper — thin wrapper around wp_options
// ---------------------------------------------------------------------------

class BDSK_Settings {

	private const OPTION_KEY = 'bdsk_settings';

	private static array $defaults = [
		'enabled'                => false,
		'read_access_enabled'    => false,
		'backup_export_enabled'  => false,
		'allowed_ips'            => '',
		'disable_ip_check'       => false,
		'api_key_hash'           => '',   // legacy: hash-only fallback (no longer written by new UI)
		'debug_log_enabled'      => false,
		'last_successful_request' => '',
		'last_failed_request'    => '',
		'last_export_downloaded' => '',
	];

	public static function get( string $key, mixed $default = null ): mixed {
		$settings = get_option( self::OPTION_KEY, [] );
		if ( isset( $settings[ $key ] ) ) {
			return $settings[ $key ];
		}
		return $default ?? self::$defaults[ $key ] ?? null;
	}

	public static function set( string $key, mixed $value ): void {
		$settings         = get_option( self::OPTION_KEY, [] );
		$settings[ $key ] = $value;
		update_option( self::OPTION_KEY, $settings, false );
	}

	public static function all(): array {
		return array_merge( self::$defaults, get_option( self::OPTION_KEY, [] ) );
	}

	public static function bulk_update( array $data ): void {
		$settings = array_merge( get_option( self::OPTION_KEY, [] ), $data );
		update_option( self::OPTION_KEY, $settings, false );
	}
}

// ---------------------------------------------------------------------------
// Security / request validation middleware
// ---------------------------------------------------------------------------

class BDSK_Security {

	/** wp_option key for the AES-256-CBC encrypted secret blob. */
	private const ENC_OPTION = 'bdsk_api_secret_enc';

	// -----------------------------------------------------------------------
	// Encryption helpers (private)
	// -----------------------------------------------------------------------

	/**
	 * Derives a 32-byte AES key from this site's auth salt.
	 * Deterministic per installation — no extra configuration required.
	 */
	private static function derive_enc_key(): string {
		// HMAC rather than plain hash so the domain separation string ('|bdsk|v1')
		// cannot be pre-imaged against the salt even if sha256 weaknesses emerge.
		return hash_hmac( 'sha256', 'bdsk|v1', wp_salt( 'auth' ), true );
	}

	/**
	 * Encrypts a plaintext secret string with AES-256-CBC and a random IV.
	 * Returns a base64-encoded blob: IV[16] || ciphertext.
	 */
	private static function encrypt_secret( string $plaintext ): string {
		$key        = self::derive_enc_key();
		$iv         = openssl_random_pseudo_bytes( 16 );
		$ciphertext = openssl_encrypt( $plaintext, 'AES-256-CBC', $key, OPENSSL_RAW_DATA, $iv );
		return base64_encode( $iv . $ciphertext );
	}

	/**
	 * Decrypts a blob produced by encrypt_secret().
	 * Returns the plaintext string, or false if decryption fails
	 * (e.g. wp_salt was rotated after the key was stored).
	 */
	private static function decrypt_secret( string $blob ): string|false {
		$raw = base64_decode( $blob, true );
		if ( false === $raw || strlen( $raw ) <= 16 ) {
			return false;
		}
		$iv         = substr( $raw, 0, 16 );
		$ciphertext = substr( $raw, 16 );
		$key        = self::derive_enc_key();
		$plaintext  = openssl_decrypt( $ciphertext, 'AES-256-CBC', $key, OPENSSL_RAW_DATA, $iv );
		return ( false !== $plaintext && '' !== $plaintext ) ? $plaintext : false;
	}

	// -----------------------------------------------------------------------
	// API secret — public interface
	// -----------------------------------------------------------------------

	/**
	 * Returns the plaintext API secret from whichever source is active.
	 *
	 * Priority:
	 *   1. BDSK_API_SECRET PHP constant (backward compat — no DB read).
	 *   2. AES-256-CBC encrypted option 'bdsk_api_secret_enc'.
	 *   3. null — no secret configured.
	 */
	public static function get_api_secret(): ?string {
		if ( defined( 'BDSK_API_SECRET' ) && '' !== BDSK_API_SECRET ) {
			return BDSK_API_SECRET;
		}

		$blob = get_option( self::ENC_OPTION, '' );
		if ( '' !== $blob ) {
			$plaintext = self::decrypt_secret( $blob );
			if ( false !== $plaintext ) {
				return $plaintext;
			}
			// Decryption failed — salt was probably rotated; treat as unconfigured.
		}

		return null;
	}

	/**
	 * Returns true if any secret source is configured
	 * (constant, encrypted option, or legacy hash).
	 */
	public static function has_secret(): bool {
		if ( defined( 'BDSK_API_SECRET' ) && '' !== BDSK_API_SECRET ) {
			return true;
		}
		if ( '' !== get_option( self::ENC_OPTION, '' ) ) {
			return true;
		}
		// Legacy hash-only fallback counts as "has secret" for API auth
		if ( '' !== BDSK_Settings::get( 'api_key_hash', '' ) ) {
			return true;
		}
		return false;
	}

	/** Returns true when the PHP constant takes priority over the stored secret. */
	public static function using_constant(): bool {
		return defined( 'BDSK_API_SECRET' ) && '' !== BDSK_API_SECRET;
	}

	/** Returns true when the encrypted-at-rest option is the active source. */
	public static function using_encrypted_option(): bool {
		return ! self::using_constant() && '' !== get_option( self::ENC_OPTION, '' );
	}

	/**
	 * Generates a cryptographically random 32-byte secret (hex-encoded = 64 chars),
	 * encrypts it, stores it, and returns the plaintext for one-time display.
	 * Safe to call for both initial generation and regeneration.
	 */
	public static function generate_and_store(): string {
		$plaintext = bin2hex( openssl_random_pseudo_bytes( 32 ) );
		$blob      = self::encrypt_secret( $plaintext );
		update_option( self::ENC_OPTION, $blob, false );
		return $plaintext;
	}

	// -----------------------------------------------------------------------
	// API key validation
	// -----------------------------------------------------------------------

	/**
	 * Constant-time key comparison supporting all three secret sources.
	 */
	public static function validate_api_key( string $provided_key ): bool {
		if ( '' === $provided_key ) {
			return false;
		}

		// 1. PHP constant (backward compat)
		if ( self::using_constant() ) {
			return hash_equals( BDSK_API_SECRET, $provided_key );
		}

		// 2. Encrypted-at-rest: compare decrypted plaintext against provided key
		$blob = get_option( self::ENC_OPTION, '' );
		if ( '' !== $blob ) {
			$plaintext = self::decrypt_secret( $blob );
			if ( false !== $plaintext ) {
				return hash_equals( $plaintext, $provided_key );
			}
		}

		// 3. Legacy hash-only fallback (old installs that set api_key_hash manually)
		$stored_hash = BDSK_Settings::get( 'api_key_hash', '' );
		if ( '' !== $stored_hash ) {
			return hash_equals( $stored_hash, hash( 'sha256', $provided_key ) );
		}

		return false;
	}

	// -----------------------------------------------------------------------
	// IP validation
	// -----------------------------------------------------------------------

	/**
	 * Returns the requester's IP.
	 *
	 * Note: X-Forwarded-For is checked only as a fallback when REMOTE_ADDR is
	 * missing or empty. XFF is spoofable unless your reverse proxy is configured
	 * to overwrite (not append) it — enable "disable_ip_check" in local dev if
	 * your proxy setup makes this unreliable.
	 */
	public static function get_request_ip( WP_REST_Request $request ): string {
		// phpcs:ignore WordPress.Security.ValidatedSanitizedInput
		$remote = $_SERVER['REMOTE_ADDR'] ?? '';

		if ( '' !== $remote ) {
			return sanitize_text_field( $remote );
		}

		// Fallback: first IP in X-Forwarded-For (may be set by load balancer / Cloudflare)
		$xff = $request->get_header( 'X-Forwarded-For' );
		if ( $xff ) {
			$parts = explode( ',', $xff );
			return trim( sanitize_text_field( $parts[0] ) );
		}

		return '';
	}

	public static function validate_ip( string $ip ): bool {
		if ( BDSK_Settings::get( 'disable_ip_check' ) ) {
			return true;
		}

		$allowed_raw = BDSK_Settings::get( 'allowed_ips', '' );
		if ( '' === $allowed_raw ) {
			return false; // no IPs configured = deny all
		}

		$allowed = array_filter( array_map( 'trim', explode( ',', $allowed_raw ) ) );

		foreach ( $allowed as $allowed_ip ) {
			if ( $ip === $allowed_ip ) {
				return true;
			}
		}

		return false;
	}

	// -----------------------------------------------------------------------
	// Main request validation middleware
	// -----------------------------------------------------------------------

	/**
	 * Returns true on success, WP_Error on failure.
	 * Logs every request (accepted and rejected) to bdsk_request_log.
	 */
	public static function validate_request( WP_REST_Request $request ): true|WP_Error {
		$start    = microtime( true );
		$endpoint = $request->get_route();
		$ip       = self::get_request_ip( $request );

		$reject = static function ( string $reason ) use ( $endpoint, $ip, $start ): WP_Error {
			$duration_ms = (int) round( ( microtime( true ) - $start ) * 1000 );
			BDSK_DB::log_request( $endpoint, $ip, 'rejected', $reason, $duration_ms );
			BDSK_Settings::set( 'last_failed_request', current_time( 'mysql', true ) );
			bdsk_log( "Request rejected: {$reason}", [ 'endpoint' => $endpoint, 'ip' => $ip ] );
			return new WP_Error( $reason, 'Request rejected.', [ 'status' => 403 ] );
		};

		// 1. Connector enabled?
		if ( ! BDSK_Settings::get( 'enabled' ) ) {
			return $reject( 'connector_disabled' );
		}

		// 2. Read access enabled?
		if ( ! BDSK_Settings::get( 'read_access_enabled' ) ) {
			return $reject( 'read_access_disabled' );
		}

		// 3. Authorization header — Bearer token
		$auth = $request->get_header( 'Authorization' );
		if ( ! $auth ) {
			// Also accept X-BDSK-Key for simpler clients
			$auth = $request->get_header( 'X-BDSK-Key' );
			$key  = $auth ? trim( $auth ) : '';
		} else {
			$key = trim( str_replace( 'Bearer ', '', $auth ) );
		}

		if ( '' === $key || ! self::validate_api_key( $key ) ) {
			return $reject( 'bad_key' );
		}

		// 4. IP check
		if ( ! self::validate_ip( $ip ) ) {
			return $reject( 'ip_mismatch' );
		}

		// 5. Log accepted request
		$duration_ms = (int) round( ( microtime( true ) - $start ) * 1000 );
		BDSK_DB::log_request( $endpoint, $ip, 'accepted', null, $duration_ms );
		BDSK_Settings::set( 'last_successful_request', current_time( 'mysql', true ) );

		return true;
	}
}
