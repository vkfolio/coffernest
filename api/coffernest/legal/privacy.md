# Privacy policy

_Last updated: 15 October 2026_

Coffernest is a household money tracker. This policy covers the Coffernest iPhone app and the
open-source Coffernest server. It is published by vignesh karnika (vkfolio), the developer of
Coffernest.

## The short version

- Using Coffernest on your iPhone needs no account. There are no analytics, no advertising and
  no tracking.
- Your entries stay on your iPhone, or on a Coffernest server that you choose.
- We, the developer, never receive your financial data, except on the one server we run
  ourselves, described below, and only if you have an account there.
- The optional assistant sends what it needs to the AI provider that you, or your server's
  operator, choose. It does this only after you agree.

## Where your data lives

**On your iPhone only.** If you choose "Use on this iPhone", everything you enter is stored on
the device, inside the app's private storage. That covers accounts, entries, bills, loans, cards,
people and categories. It is included in your encrypted device backups and is removed when you
delete the app or use Settings → Erase all data.

**On a Coffernest server.** If you connect to a server, your data is stored on that server. The
server's operator, which may be you, controls it. Most Coffernest servers are run by the people
who use them. The operator decides how long data is kept and who can reach the machine. Members
of a household can see the household's data according to their role.

**On the server we run.** The developer runs one Coffernest server, at
https://coffernest.vkfolio.com. It holds a demo household of made-up data, which anyone can sign
in to and which is reset regularly, and the households of people its admin has added. It is not
open for public sign-up. If you have an account there, the developer is its operator: your data
is used only to run Coffernest for you, is never sold or shared, and is deleted when you delete
your account or household.

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
- **Your server's assistant**: if the server's operator has set one up, it can answer questions,
  read statements you upload and sort entries into categories. For that, your question and the
  figures needed to answer it, the statements you upload, and the payee, amount and date of
  entries to sort are sent from the server to the provider the operator chose (for example
  Anthropic or OpenAI). The app asks for your permission first, and you can withdraw it in
  Settings → Assistant. The server we run has no assistant.
- **Your own API key** (Advanced): the app sends your question and the figures needed directly
  to the provider you picked, using your key. The app asks for permission first. Full account
  numbers and your sign-in details are never sent.

Those providers handle data under their own privacy policies.

## Purchases

The free trial and the one-time unlock are bought through Apple's App Store. Apple handles the
payment; we receive no card or bank details, only Apple's confirmation of what was bought.

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

Questions about this policy: e-mail me@vkfolio.com, or open an issue at
https://github.com/vkfolio/coffernest/issues. Report security problems privately as
described in SECURITY.md in that repository.

We will post any change to this policy here and update the date above.
