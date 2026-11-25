"""Admin command line (use this if you forget the Admin password).

    python manage.py create-admin           # create the single Admin if one does not exist
    python manage.py reset-password <user>  # set a new password for any user
"""
import getpass
import sys

import database
import security


def ask_password():
    pw = getpass.getpass("New password (min 8 chars): ")
    if len(pw) < 8 or pw != getpass.getpass("Repeat password: "):
        sys.exit("The password is too short or the passwords do not match.")
    return pw


def main(argv):
    database.init_db()
    if argv[:1] == ["create-admin"]:
        if database.count_admins():
            sys.exit("An Admin account already exists.")
        username = input("Username: ").strip()
        if len(username) < 3 or database.get_user_by_username(username):
            sys.exit("The username is too short or already exists.")
        database.create_user(username, input("Full name: ").strip() or username,
                             security.hash_password(ask_password()), "admin")
        print("Admin created.")
    elif len(argv) == 2 and argv[0] == "reset-password":
        user = database.get_user_by_username(argv[1])
        if not user:
            sys.exit("User not found.")
        database.update_user(user["id"], password_hash=security.hash_password(ask_password()), active=1)
        database.delete_user_sessions(user["id"])
        database.log_action("cli", "password_reset", user["username"])
        print("Password reset successfully.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
