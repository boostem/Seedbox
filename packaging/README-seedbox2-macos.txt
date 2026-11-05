Seedbox 2 needs macOS {{MIN_MACOS}} or newer and an internet connection for its models the first time.

To install it, drag Seedbox 2 onto Applications.

This build is signed ad-hoc, not by a registered Apple developer, so macOS won't open it with a double click the
first time. Right-click Seedbox 2 in Applications, choose Open, and confirm. If macOS says it is damaged, or on
macOS 15 and newer if Open isn't offered, run this in Terminal once:

    xattr -dr com.apple.quarantine "/Applications/Seedbox 2.app"

Seedbox 2 keeps your settings in ~/Library/Application Support/Seedbox 2 and its caches, with the downloaded models,
in ~/Library/Caches/Seedbox 2. Third-party notices are in Seedbox 2.app/Contents/Resources/licenses.
