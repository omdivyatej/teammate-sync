// electron-builder afterSign hook: notarize the signed .app with Apple's
// notary service, then electron-builder staples the ticket into the dmg.
//
// Credentials come from a keychain profile (no secrets in the repo or env).
// Create it once:
//   xcrun notarytool store-credentials codebaton-notary \
//     --apple-id "you@example.com" --team-id 9XYUF82Y5X
// (notarytool prompts for an app-specific password, entered securely.)
//
// Set NOTARIZE=0 to skip (e.g. fast local test builds).

const { notarize } = require('@electron/notarize');

const KEYCHAIN_PROFILE = process.env.NOTARIZE_PROFILE || 'codebaton-notary';

exports.default = async function notarizing(context) {
  const { electronPlatformName, appOutDir } = context;
  if (electronPlatformName !== 'darwin') return;
  if (process.env.NOTARIZE === '0') {
    console.log('  • notarize: skipped (NOTARIZE=0)');
    return;
  }

  const appName = context.packager.appInfo.productFilename;
  const appPath = `${appOutDir}/${appName}.app`;

  console.log(`  • notarizing ${appName}.app via profile "${KEYCHAIN_PROFILE}" (this can take a few minutes)…`);
  await notarize({
    tool: 'notarytool',
    appPath,
    keychainProfile: KEYCHAIN_PROFILE,
  });
  console.log('  • notarization complete');
};
