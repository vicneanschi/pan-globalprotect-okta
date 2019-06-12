#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
   The MIT License (MIT)
   
   Copyright (C) 2018 Andris Raugulis (moo@arthepsy.eu)
   
   Permission is hereby granted, free of charge, to any person obtaining a copy
   of this software and associated documentation files (the "Software"), to deal
   in the Software without restriction, including without limitation the rights
   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
   copies of the Software, and to permit persons to whom the Software is
   furnished to do so, subject to the following conditions:
   
   The above copyright notice and this permission notice shall be included in
   all copies or substantial portions of the Software.
   
   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
   THE SOFTWARE.
"""
from __future__ import print_function
import io, os, sys, re, json, base64, getpass, subprocess, shlex, signal
from lxml import etree
import requests
import tempfile

if sys.version_info >= (3,):
	from urllib.parse import urlparse
	from urllib.parse import urljoin
	text_type = str
	binary_type = bytes
else:
	from urlparse import urlparse
	from urlparse import urljoin
	text_type = unicode
	binary_type = str
	input = raw_input

to_b = lambda v: v if isinstance(v, binary_type) else v.encode('utf-8')
to_u = lambda v: v if isinstance(v, text_type) else v.decode('utf-8')


def log(s):
	print('[INFO] {0}'.format(s))

def dbg(d, h, *xs):
	if not d:
		return
	print('# {0}:'.format(h))
	for x in xs:
		print(x)
	print('---')

def err(s):
	print('err: {0}'.format(s))
	sys.exit(1)

def parse_xml(xml):
	try:
		xml = bytes(bytearray(xml, encoding='utf-8'))
		parser = etree.XMLParser(ns_clean=True, recover=True)
		return etree.fromstring(xml, parser)
	except:
		err('failed to parse xml')

def parse_html(html):
	try:
		parser = etree.HTMLParser()
		return etree.fromstring(html, parser)
	except:
		err('failed to parse html')

def parse_rjson(r):
	try:
		return r.json()
	except:
		err('failed to parse json')

def parse_form(html, current_url = None):
	xform = html.find('.//form')
	url = xform.attrib.get('action', '').strip()
	if not url.startswith('http') and current_url:
		url = urljoin(current_url, url)
	data = {}
	for xinput in html.findall('.//input'):
		k = xinput.attrib.get('name', '').strip()
		v = xinput.attrib.get('value', '').strip()
		if len(k) > 0 and len(v) > 0:
			data[k] = v
	return url, data


def load_conf(cf):
	log('load conf')
	conf = {}
	keys = ['vpn_url', 'username', 'password', 'okta_url']
	line_nr = 0
	with io.open(cf, 'r', encoding='utf-8') as fp:
		for rline in fp:
			line_nr += 1
			line = rline.strip()
			mx = re.match(r'^\s*([^=\s]+)\s*=\s*(.*?)\s*(?:#\s+.*)?\s*$', line)
			if mx:
				k, v = mx.group(1).lower(), mx.group(2)
				if k.startswith('#'):
					continue
				for q in '"\'':
					if re.match(r'^{0}.*{0}$'.format(q), v):
						v = v[1:-1]
				conf[k] = v
				conf['{0}.line'.format(k)] = line_nr
	for k, v in os.environ.items():
		k = k.lower()
		if k.startswith('gp_'):
			k = k[3:]
			if len(k) == 0:
				continue
			conf[k] = v.strip()
	if len(conf.get('username', '').strip()) == 0:
		conf['username'] = input('username: ').strip()
	if len(conf.get('password', '').strip()) == 0:
		conf['password'] = getpass.getpass('password: ').strip()
	if len(conf.get('openconnect_certs', '').strip()) == 0:
		conf['openconnect_certs'] = tempfile.NamedTemporaryFile()
		log('will collect openconnect_certs in temporary file: {0} and verify against them'.format(conf['openconnect_certs'].name))
	else:
		conf['openconnect_certs'] = open(conf['openconnect_certs'], 'wb')
	if len(conf.get('vpn_url_cert', '').strip()) != 0:
		if not os.path.exists(conf.get('vpn_url_cert')):
			err('configured vpn_url_cert file does not exist')
		conf['openconnect_certs'].write(open(conf.get('vpn_url_cert'), 'rb').read()) # copy it
	if len(conf.get('okta_url_cert', '').strip()) != 0:
		if not os.path.exists(conf.get('okta_url_cert')):
			err('configured okta_url_cert file does not exist')
		conf['openconnect_certs'].write(open(conf.get('okta_url_cert'), 'rb').read()) # copy it
	if len(conf.get('client_cert', '').strip()) != 0:
		if not os.path.exists(conf.get('client_cert')):
				err('configured client_cert file does not exist')
	for k in keys:
		if k not in conf:
			err('missing configuration key: {0}'.format(k))
		else:
			if len(conf[k].strip()) == 0:
				err('empty configuration key: {0}'.format(k))
	conf['debug'] = conf.get('debug', '').lower() in ['1', 'true']
	conf['openconnect_certs'].flush()
	return conf

def mfa_priority(conf, ftype, fprovider):
	if ftype == 'token:software:totp':
		ftype = 'totp'
	if ftype not in ['totp', 'sms']:
		return 0
	mfa_order = conf.get('mfa_order', '')
	if ftype in mfa_order:
		priority = (10 - mfa_order.index(ftype)) * 100
	else:
		priority = 0
	value = conf.get('{0}.{1}'.format(ftype, fprovider))
	if ftype == 'sms':
		if not (value or '').lower() in ['1', 'true']:
			value = None
	line_nr = conf.get('{0}.{1}.line'.format(ftype, fprovider), 0)
	if value is None:
		priority += 0
	elif len(value) == 0:
		priority += (128 - line_nr)
	else:
		priority += (512 - line_nr)
	return priority

def get_state_token(conf, c, current_url = None):
	rx_state_token = re.search(r'var\s*stateToken\s*=\s*\'([^\']+)\'', c)
	if not rx_state_token:
		dbg(conf.get('debug'), 'not found', 'stateToken')
		return None
	state_token = to_b(rx_state_token.group(1)).decode('unicode_escape').strip()
	return state_token

def get_redirect_url(conf, c, current_url = None):
	rx_base_url = re.search(r'var\s*baseUrl\s*=\s*\'([^\']+)\'', c)
	rx_from_uri = re.search(r'var\s*fromUri\s*=\s*\'([^\']+)\'', c)
	if not rx_from_uri:
		dbg(conf.get('debug'), 'not found', 'formUri')
		return None
	from_uri = to_b(rx_from_uri.group(1)).decode('unicode_escape').strip()
	if from_uri.startswith('http'):
		return from_uri
	if not rx_base_url:
		dbg(conf.get('debug'), 'not found', 'baseUri')
		if current_url:
			return urljoin(current_url, from_uri)
		return from_uri
	base_url = to_b(rx_base_url.group(1)).decode('unicode_escape').strip()
	return base_url + from_uri

def send_req(conf, s, name, url, data, **kwargs):
	dbg(conf.get('debug'), '{0}.request'.format(name), url)
	if kwargs.get('expected_url'):
		purl = list(urlparse(url))
		purl = (purl[0], purl[1].split(':')[0])
		pexp = list(urlparse(kwargs.get('expected_url')))
		pexp = (pexp[0], pexp[1].split(':')[0])
		if purl != pexp: 
			err('{0}: unexpected url found {1} != {2}'.format(name, purl, pexp))
	do_json = True if kwargs.get('json') else False
	headers = {}
	if do_json:
		data = json.dumps(data)
		headers['Accept'] = 'application/json'
		headers['Content-Type'] = 'application/json'
	if kwargs.get('get'):
		r = s.get(url, headers=headers,
			verify=kwargs.get('verify', True))
	else:
		r = s.post(url, data=data, headers=headers,
			verify=kwargs.get('verify', True))
	hdump = '\n'.join([k + ': ' + v for k, v in sorted(r.headers.items())])
	rr = 'status: {0}\n\n{1}\n\n{2}'.format(r.status_code, hdump, r.text)
	if r.status_code != 200:
		err('okta {0} request failed. {0}'.format(rr))
	dbg(conf.get('debug'), '{0}.response'.format(name), rr)
	if do_json:
		return r.headers, parse_rjson(r)
	return r.headers, r.text


def paloalto_prelogin(conf, s, again=False):
	log('prelogin request [vpn_url]')
	if again:
		url = '{0}/ssl-vpn/prelogin.esp'.format(conf.get('vpn_url'))
	else:
		url = '{0}/global-protect/prelogin.esp'.format(conf.get('vpn_url'))
	h, c = send_req(conf, s, 'prelogin', url, {}, get=True,
		verify=conf.get('vpn_url_cert'))
	x = parse_xml(c)
	saml_req = x.find('.//saml-request')
	if saml_req is None:
		msg = x.find('.//msg')
		msg = msg.text.strip() if msg is not None else 'Probably you need a certificate?'
		err('did not find saml request. {0}'.format(msg))
	if len(saml_req.text.strip()) == 0:
		err('empty saml request')
	try:
		saml_raw = base64.b64decode(saml_req.text)
	except:
		err('failed to decode saml request')
	dbg(conf.get('debug'), 'prelogin.decoded', saml_raw)
	saml_xml = parse_html(saml_raw)
	return saml_xml

def okta_saml(conf, s, saml_xml):
	log('okta saml request [okta_url]')
	url, data = parse_form(saml_xml)
	h, c = send_req(conf, s, 'saml', url, data,
		expected_url=conf.get('okta_url'), verify=conf.get('okta_url_cert'))
	redirect_url = get_redirect_url(conf, c, url)
	if redirect_url is None:
		err('did not find redirect url')
	return redirect_url

def okta_auth(conf, s, stateToken = None):
	log('okta auth request [okta_url]')
	url = '{0}/api/v1/authn'.format(conf.get('okta_url'))
	data = {
		'username': conf.get('username'),
		'password': conf.get('password'),
		'options': {
			'warnBeforePasswordExpired':True,
			'multiOptionalFactorEnroll':True
		}
	} if stateToken is None else {
		'stateToken': stateToken
	}
	h, j = send_req(conf, s, 'auth', url, data, json=True,
		verify=conf.get('okta_url_cert'))

	while True:
		ok, r = okta_transaction_state(conf, s, j)
		if ok == True:
			return r
		j = r

def okta_transaction_state(conf, s, j):
	# https://developer.okta.com/docs/api/resources/authn#transaction-state
	status = j.get('status', '').strip().lower()
	dbg(conf.get('debug'), 'status', status)
	# status: unauthenticated
	# status: password_warn
	if status == 'password_warn':
		log('password expiration warning')
		url = j.get('_links', {}).get('skip', {}).get('href', '').strip()
		if len(url) == 0:
			err('skip url not found')
		state_token = j.get('stateToken', '').strip()
		if len(state_token) == 0:
			err('empty state token')
		data = {'stateToken': state_token}
		h, j = send_req(conf, s, 'skip', url, data, json=True,
			expected_url=conf.get('okta_url'), verify=conf.get('okta_url_cert'))
		return False, j
	# status: password_expired
	# status: recovery
	# status: recovery_challenge
	# status: password_reset
	# status: locked_out
	# status: mfa_enroll
	# status: mfa_enroll_activate
	# status: mfa_required
	if status == 'mfa_required':
		j = okta_mfa(conf, s, j)
		return False, j
	# status: mfa_challenge
	# status: success
	if status == 'success':
		session_token = j.get('sessionToken', '').strip()
		if len(session_token) == 0:
			err('empty session token')
		return True, session_token
	print(j)
	err('unknown status: {0}'.format(status))

def okta_mfa(conf, s, j):
	state_token = j.get('stateToken', '').strip()
	if len(state_token) == 0:
		err('empty state token')
	factors_json = j.get('_embedded', {}).get('factors', [])
	if len(factors_json) == 0:
		err('no factors found')
	factors = []
	for factor in factors_json:
		factor_id = factor.get('id', '').strip()
		factor_type = factor.get('factorType', '').strip().lower()
		provider = factor.get('provider', '').strip().lower()
		factor_url = factor.get('_links', {}).get('verify', {}).get('href')
		if len(factor_type) == 0 or len(provider) == 0 or len(factor_url) == 0:
			continue
		factors.append({
			'id': factor_id,
			'type': factor_type,
			'provider': provider,
			'priority': mfa_priority(conf, factor_type, provider),
			'url': factor_url})
	dbg(conf.get('debug'), 'factors', factors)
	if len(factors) == 0:
		err('no factors found')
	for f in sorted(factors, key=lambda x: x.get('priority', 0), reverse=True):
		#print(f)
		ftype = f.get('type')
		if ftype == 'token:software:totp':
			r = okta_mfa_totp(conf, s, f, state_token)
		elif ftype == 'sms':
			r = okta_mfa_sms(conf, s, f, state_token)
		else:
			r = None
		if r is not None:
			return r
	err('no factors processed')

def okta_mfa_totp(conf, s, factor, state_token):
	provider = factor.get('provider', '')
	secret = conf.get('totp.{0}'.format(provider), '') or ''
	code = None
	if len(secret) == 0:
		code = input('{0} TOTP: '.format(provider)).strip()
	else:
		try:
			import pyotp
		except ImportError:
			err('Need pyotp package, consider doing \'pip install pyotp\' (or similar)')
		totp = pyotp.TOTP(secret)
		code = totp.now()
	code = code or ''
	if len(code) == 0:
		return None
	data = {
		'factorId': factor.get('id'),
		'stateToken': state_token,
		'passCode': code
	}
	log('mfa {0} totp request: {1} [okta_url]'.format(provider, code))
	h, j = send_req(conf, s, 'totp mfa', factor.get('url'), data, json=True,
		expected_url=conf.get('okta_url'), verify=conf.get('okta_url_cert'))
	return j

def okta_mfa_sms(conf, s, factor, state_token):
	provider = factor.get('provider', '')
	data = {
		'factorId': factor.get('id'),
		'stateToken': state_token,
	}
	log('mfa {0} sms request [okta_url]'.format(provider))
	h, j = send_req(conf, s, 'sms mfa (1)', factor.get('url'), data, json=True,
		expected_url=conf.get('okta_url'), verify=conf.get('okta_url_cert'))
	code = input('{0} SMS verification code: '.format(provider)).strip()
	if len(code) == 0:
		return None
	data['passCode'] = code
	log('mfa {0} sms request [okta_url]'.format(provider))
	h, j = send_req(conf, s, 'sms mfa (2)', factor.get('url'), data, json=True,
		expected_url=conf.get('okta_url'), verify=conf.get('okta_url_cert'))
	return j

def okta_redirect(conf, s, session_token, redirect_url):
	rc = 0
	form_url, form_data = None, {}
	while True:
		if rc > 10:
			err('redirect rabbit hole is too deep...')
		rc += 1
		if redirect_url:
			data = {
				'checkAccountSetupComplete': 'true',
				'report': 'true',
				'token': session_token,
				'redirectUrl': redirect_url
			}
			url = '{0}/login/sessionCookieRedirect'.format(conf.get('okta_url'))
			log('okta redirect request {0} [okta_url]'.format(rc))
			h, c = send_req(conf, s, 'redirect', url, data,
				verify=conf.get('okta_url_cert'))
			state_token = get_state_token(conf, c, url)
			redirect_url = get_redirect_url(conf, c, url)
			if redirect_url:
				form_url, form_data = None, {}
			else:
				xhtml = parse_html(c)
				form_url, form_data = parse_form(xhtml, url)
			if state_token is not None:
				log('stateToken: {0}'.format(state_token))
				okta_auth(conf, s, state_token)
		elif form_url:
			log('okta redirect form request [vpn_url]')
			h, c = send_req(conf, s, 'redirect form', form_url, form_data,
				expected_url=conf.get('vpn_url'), verify=conf.get('vpn_url_cert'))
		saml_username = h.get('saml-username', '').strip()
		prelogin_cookie = h.get('prelogin-cookie', '').strip()
		if saml_username and prelogin_cookie:
			saml_auth_status = h.get('saml-auth-status', '').strip()
			saml_slo = h.get('saml-slo', '').strip()
			dbg(conf.get('debug'), 'saml prop', [saml_auth_status, saml_slo])
			return saml_username, prelogin_cookie

def paloalto_getconfig(conf, s, saml_username, prelogin_cookie):
	log('getconfig request [vpn_url]')
	url = '{0}/global-protect/getconfig.esp'.format(conf.get('vpn_url'))
	data = {
		'user': saml_username,
		'passwd': '',
		'inputStr': '',
		'clientVer': '4100',
		'clientos': 'Windows',
		'clientgpversion': '4.1.0.98',
		'computer': 'DESKTOP',
		'os-version': 'Microsoft Windows 10 Pro, 64-bit',
		# 'host-id': '00:11:22:33:44:55'
		'prelogin-cookie': prelogin_cookie,
		'ipv6-support': 'yes'
	}
	h, c = send_req(conf, s, 'getconfig', url, data,
		verify=conf.get('vpn_url_cert'))
	x = parse_xml(c)
	xtmp = x.find('.//portal-userauthcookie')
	if xtmp is None:
		err('did not find portal-userauthcookie')
	portal_userauthcookie = xtmp.text
	if len(portal_userauthcookie) == 0:
		err('empty portal_userauthcookie')
	gateway = x.find('.//gateways//entry').get('name')
	for entry in x.find('.//root-ca'):
		cert = entry.find('.//cert').text
		conf['openconnect_certs'].write(to_b(cert))
	conf['openconnect_certs'].flush()
	return portal_userauthcookie, gateway

# Combined first half of okta_saml with second half of okta_redirect
def okta_saml_2(conf, s, saml_xml):
	log('okta saml request')
	url, data = parse_form(saml_xml)
	r = s.post(url, data=data)
	if r.status_code != 200:
		err('redirect request failed. {0}'.format(reprr(r)))
	dbg(conf.get('debug'), 'redirect.response', r.status_code, r.text)
	xhtml = parse_html(r.text)

	url, data = parse_form(xhtml)
	log('okta redirect form request')
	r = s.post(url, data=data)
	if r.status_code != 200:
		err('redirect form request failed. {0}'.format(reprr(r)))
	dbg(conf.get('debug'), 'form.response', r.status_code, r.text)
	saml_username = r.headers.get('saml-username', '').strip()
	if len(saml_username) == 0 and not again:
		err('saml-username empty')
	#saml_auth_status = r.headers.get('saml-auth-status', '').strip()
	#saml_slo = r.headers.get('saml-slo', '').strip()
	prelogin_cookie = r.headers.get('prelogin-cookie', '').strip()
	if len(prelogin_cookie) == 0:
		err('prelogin-cookie empty')
	return saml_username, prelogin_cookie

def main():
	if len(sys.argv) < 2:
		print('usage: {0} <conf>'.format(sys.argv[0]))
		sys.exit(1)

	conf = load_conf(sys.argv[1])
	
	s = requests.Session()

	if conf.get('client_cert'):
		s.cert = conf.get('client_cert')

	s.headers['User-Agent'] = 'PAN GlobalProtect'
	saml_xml = paloalto_prelogin(conf, s)
	redirect_url = okta_saml(conf, s, saml_xml)
	token = okta_auth(conf, s)
	log('sessionToken: {0}'.format(token))
	saml_username, prelogin_cookie = okta_redirect(conf, s, token, redirect_url)
	userauthcookie, gateway = paloalto_getconfig(conf, s, saml_username, prelogin_cookie)

	log('portal-userauthcookie: {0}'.format(userauthcookie))
	log('gateway: {0}'.format(gateway))

	# Another dance?
	if conf.get('another_dance', '').lower() in ['1', 'true']:
		saml_xml = paloalto_prelogin(conf, s, again=True)
		saml_username, prelogin_cookie = okta_saml_2(conf, s, saml_xml)

	log('saml-username: {0}'.format(saml_username))
	log('prelogin-cookie: {0}'.format(prelogin_cookie))

	if userauthcookie == 'empty' and prelogin_cookie != 'empty':
	    cookie_type = "gateway:prelogin-cookie"
	    cookie = prelogin_cookie
	else:
	    cookie_type = "portal:portal-userauthcookie"
	    cookie = userauthcookie
	
	username = saml_username
	cmd = conf.get('openconnect_cmd', 'openconnect')
	cmd += ' --protocol=gp -u \'{0}\''
	cmd += ' --usergroup {1}'
	if conf.get('client_cert'):
		cmd += ' --certificate=\'{0}\''.format(conf.get('client_cert'))
	if conf.get('openconnect_certs') and os.path.getsize(conf.get('openconnect_certs').name) > 0:
		cmd += ' --cafile=\'{0}\''.format(conf.get('openconnect_certs').name)
	cmd += ' --passwd-on-stdin ' + conf.get('openconnect_args', '') + ' \'{2}\''
	cmd = cmd.format(username, cookie_type, conf.get('vpn_url'))
	gw = conf.get('gateway', gateway).strip()
	bugs = ''
	if conf.get('bug.nl', '').lower() in ['1', 'true']:
		bugs += '\\n'
	if conf.get('bug.username', '').lower() in ['1', 'true']:
		bugs += '{0}\\n'.format(username.replace('\\', '\\\\'))
	if len(gw) > 0:
		pcmd = 'printf \'' + bugs + '{0}\\n{1}\''.format(cookie, gw)
	else:
		pcmd = 'printf \'' + bugs + '{0}\''.format(cookie)
	print()
	conf['openconnect_certs'].close()
	if conf.get('execute', '').lower() in ['1', 'true']:
		cmd = shlex.split(cmd)
		cmd = [os.path.expandvars(os.path.expanduser(x)) for x in cmd]
		pp = subprocess.Popen(shlex.split(pcmd), stdout=subprocess.PIPE)
		cp = subprocess.Popen(cmd, stdin=pp.stdout, stdout=sys.stdout)
		pp.stdout.close()
		# Do not abort on SIGINT. openconnect will perform proper exit & cleanup
		signal.signal(signal.SIGINT, signal.SIG_IGN)
		cp.communicate()
	else:
		print('{0} | {1}'.format(pcmd, cmd))


if __name__ == '__main__':
	main()
