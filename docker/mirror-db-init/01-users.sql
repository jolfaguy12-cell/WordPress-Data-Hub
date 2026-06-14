-- Create import user (full privileges on mirror DB only)
CREATE USER IF NOT EXISTS 'mirror_import'@'%' IDENTIFIED BY 'importpass';
GRANT ALL PRIVILEGES ON `behdashtik_wp_mirror`.* TO 'mirror_import'@'%';
GRANT ALL PRIVILEGES ON `behdashtik_wp_mirror_staging`.* TO 'mirror_import'@'%';

-- Create read-only user for AI / reporting tools
CREATE USER IF NOT EXISTS 'mirror_readonly'@'%' IDENTIFIED BY 'readonlypass';
GRANT SELECT ON `behdashtik_wp_mirror`.* TO 'mirror_readonly'@'%';

FLUSH PRIVILEGES;
