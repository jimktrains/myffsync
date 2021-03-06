#!/usr/bin/env python3

from base64 import b64decode
from configparser import ConfigParser
import requests
from fxa.core import Client
from fxa.plugins.requests import FxABearerTokenAuth, FxABrowserIDAuth
from requests_hawk import HawkAuth
import json
from fxa.crypto import derive_key, calculate_hmac
from Crypto.Cipher import AES

from hkdf import hkdf_extract, hkdf_expand, Hkdf
import hashlib 
import hmac

config_file_name = 'config.test.ini'

config = ConfigParser()
config.read(config_file_name)

hawk_resp = None
encryption_key = None
hmac_key = None

if 'hawk' not in config or 'fxa' not in config:
    user = config['user']

    client = Client("https://api.accounts.firefox.com")
    session = client.login(user['email'], user['password'], keys=True)

    keyA,keyB = session.fetch_keys()

    info = b"identity.mozilla.com/picl/v1/oldsync"
    namespace = b"oldsync"
    keys = derive_key(secret=keyB, namespace=namespace, size=64)
    encryption_key = keys[0:32]
    hmac_key = keys[32:64]

    # TODO: Store this or a derived longer-lived token
    #       Causes a login event which causes an email
    fxab = FxABrowserIDAuth(user['email'], user['password'], with_client_state=True)
    raw_resp = requests.get('https://token.services.mozilla.com/1.0/sync/1.5', auth=fxab)
    raw_resp.raise_for_status()

    hawk_resp = raw_resp.json()
    config['hawk'] = hawk_resp
    config['fxa'] = {
        'encryption_key': encryption_key.hex(),
        'hmac_key': hmac_key.hex(),
    }
    with open(config_file_name, 'w') as configfile:
        config.write(configfile)
else:
    hawk_resp = config['hawk']
    encryption_key = bytes.fromhex(config['fxa']['encryption_key'])
    hmac_key = bytes.fromhex(config['fxa']['hmac_key'])

    if 'user' in config:
        print("You can delete the [user] section from the config")

user_id = hawk_resp['uid']
endpoint = hawk_resp['api_endpoint']
hawk_auth = HawkAuth(id=hawk_resp['id'], key=hawk_resp['key'])


#TODO: Include newest item's time
raw_resp=requests.get(f"{endpoint}/info/collections", auth=hawk_auth)
collections = raw_resp.json()

class AES_HMAC_KeyPairs:
    def __init__(self, e, h):
        self.account_keypair= (e, h)

        self.collection_keys = {}
        self.collection_keypair = None

    def set_collection_default(self, pair):
        self.collection_keypair = pair

    def get_account_default(self):
        return self.account_keypair

    def get_collection_default(self):
        if self.collection_keypair is not None:
            return self.collection_keypair
        return self.account_keypair

    def __setitem__(self, key, val):
        self.collection_keys[key] = val;

    def __getitem__(self, key):
        if key in self.collection_keys:
            return self.collection_keys[key]
        return self.get_collection_default()

keypairs = AES_HMAC_KeyPairs(encryption_key, hmac_key)

def get_collection(collection):
    raw_resp=requests.get(f"{endpoint}/storage/{collection}", auth=hawk_auth)
    items = raw_resp.json()
    for item in items:
        raw_resp=requests.get(f"{endpoint}/storage/{collection}/{item}", auth=hawk_auth)
        resp  = raw_resp.json()
        record = json.loads(resp['payload'])

        ciphertext_b64  = record['ciphertext'].encode('ascii')
        iv_b64          = record['IV']
        record_hmac     = record['hmac']

        keypair = keypairs[collection]
        encryption_key, hmac_key = keypairs[collection]

        # It appears that the Base-64 encoded ciphertext is what is HMACed.
        # https://moz-services-docs.readthedocs.io/en/latest/sync/storageformat5.html#crypto-keys-record
        hmac_comp = hmac.new(key=hmac_key, msg=ciphertext_b64, digestmod=hashlib.sha256).digest()

        hmac_comp_hex = hmac_comp.hex()
        assert(record_hmac == hmac_comp_hex)

        ciphertext = b64decode(ciphertext_b64)
        iv         = b64decode(iv_b64)

        aes = AES.new(encryption_key, AES.MODE_CBC, iv)
        contents = aes.decrypt(ciphertext)
        # removing PKS7 padding
        contents = contents[:-contents[-1]]
        yield json.loads(contents)

for keys in get_collection("crypto"):
    keypairs.set_collection_default(list(map(b64decode, keys["default"])))
    for collection, keypair in keys['collections'].items():
        keypairs[collection] = list(map(b64decode, keypair))

print("Looking for items to sync")
for collection, last_mod_time in collections.items():
    # We handle that before doing this
    if collection == "crypto":
        continue
    # This doesn't have encrypted data and can't be handled via get_collection
    if collection == "meta":
        continue
    print(f"{collection}(last modified time={last_mod_time})")
    for item in get_collection(collection):
        print(item)
