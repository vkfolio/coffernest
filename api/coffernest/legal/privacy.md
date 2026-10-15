# Privacy policy

_Last updated: 14 October 2026_

Coffernest is a household money tracker. This policy covers the Coffernest iPhone app and the
open-source Coffernest server. It is published by vkfolio, the developer of Coffernest.

## The short version

- Coffernest has no accounts with us, no analytics, no advertising and no tracking.
- Your entries stay on your iPhone, or on a Coffernest server that you or someone you trust runs.
- We, the developer, never receive your financial data.
- The optional assistant sends questions and the figures needed to answer them to the AI provider
  that you, or your server's operator, choose. It does this only after you agree.

## Where your data lives

**On your iPhone only.** If you choose "Use on this iPhone", everything you enter is stored on
the device, inside the app's private storage. That covers accounts, entries, bills, loans, cards,
people and categories. It is included in your encrypted device backups and is removed when you
delete the app or use Settings → Erase all data.

**On a Coffernest server.** If you connect to a server, your data is stored on that server. The
server's operator, which may be you, controls it. Coffernest servers are self-hosted, and the
developer does not run one for you. The operator decides how long data is kept and who can reach
the machine. Members of a household can see the household's data according to their role.

## What is stored

- **Sign-in details** (server mode only): your name, e-mail address and a password hash, plus
  optional two-step verification secrets.
- **Financial records you enter or import**: accounts and the last four digits of account
  numbers, transactions, recurring bills and income, loans, card statements, people you tag
  spending for, notes, and bills or statements you attach.
- **Optional full account numbers**: these are saved only if you type them in. On a server they
  are encrypted at rest.

Coffernest does not connect to banks, read bank credentials or SMS messages, or move money.

## The assistant (optional)

- **Apple Intelligence**: the assistant runs on your iPhone. Nothing leaves the device.
- **Your server's assistant**: if the server's operator has set one up, your question and the
  figures needed to answer it are sent from the server to the provider the operator chose (for
  example Anthropic or OpenAI). The app asks for your permission first, and you can withdraw it
  in Settings → Assistant.
- **Your own API key** (Advanced): the app sends your question and the figures needed directly
  to the provider you picked, using your key. The app asks for permission first. Full account
  numbers and your sign-in details are never sent.

Those providers handle data under their own privacy policies.

## Permissions the app asks for

- **Face ID**: to lock the app and to reveal a saved account number. Apple handles this; the app
  never sees your face data.
- **Camera**: only when you scan a bill or statement.
- **Notifications**: reminders for bills that are due. These are scheduled on the device.

## Deleting your data

- **On this iPhone**: Settings → Erase all data, or delete the app.
- **On a server**: Settings → Profile → Delete account permanently deletes your account. If you
  are the household's only member, the household and its files are deleted too. A household's
  owner can delete the whole household. Server operators can also delete data directly.

## Children

Coffernest is meant for adults managing a household. It lets you tag spending to people,
including children, but children do not use the app or have accounts.

## Your rights

You can see, correct, export (Settings → Export) and delete your data at any time. If you live in
India, the Digital Personal Data Protection Act, 2023 gives you these rights. For data on a
server, the server's operator is responsible for it. For data on your iPhone, you are.

## Contact

Questions about this policy: open an issue at
https://github.com/vkfolio/coffernest/issues. Report security problems privately as
described in SECURITY.md in that repository.

We will post any change to this policy here and update the date above.
