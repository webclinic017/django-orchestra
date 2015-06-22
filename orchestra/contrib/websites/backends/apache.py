import os
import re
import textwrap

from django.template import Template, Context
from django.utils.translation import ugettext_lazy as _

from orchestra.contrib.orchestration import ServiceController
from orchestra.contrib.resources import ServiceMonitor

from .. import settings
from ..utils import normurlpath


class Apache2Backend(ServiceController):
    """
    Apache &ge;2.4 backend with support for the following directives:
        <tt>static</tt>, <tt>location</tt>, <tt>fpm</tt>, <tt>fcgid</tt>, <tt>uwsgi</tt>, \
        <tt>ssl</tt>, <tt>security</tt>, <tt>redirects</tt>, <tt>proxies</tt>, <tt>saas</tt>
    """
    HTTP_PORT = 80
    HTTPS_PORT = 443
    
    model = 'websites.Website'
    related_models = (
        ('websites.Content', 'website'),
        ('websites.WebsiteDirective', 'website'),
        ('webapps.WebApp', 'website_set'),
    )
    verbose_name = _("Apache 2")
    doc_settings = (settings, (
        'WEBSITES_VHOST_EXTRA_DIRECTIVES',
        'WEBSITES_DEFAULT_SSL_CERT',
        'WEBSITES_DEFAULT_SSL_KEY',
        'WEBSITES_DEFAULT_SSL_CA',
        'WEBSITES_BASE_APACHE_CONF',
        'WEBSITES_DEFAULT_IPS',
        'WEBSITES_SAAS_DIRECTIVES',
    ))
    
    def render_virtual_host(self, site, context, ssl=False):
        context['port'] = self.HTTPS_PORT if ssl else self.HTTP_PORT
        context['vhost_set_fcgid'] = False
        extra_conf = self.get_content_directives(site, context)
        directives = site.get_directives()
        if ssl:
            extra_conf += self.get_ssl(directives)
        extra_conf += self.get_security(directives)
        extra_conf += self.get_redirects(directives)
        extra_conf += self.get_proxies(directives)
        extra_conf += self.get_saas(directives)
        settings_context = site.get_settings_context()
        for location, directive in settings.WEBSITES_VHOST_EXTRA_DIRECTIVES:
            extra_conf.append((location, directive % settings_context))
        # Order extra conf directives based on directives (longer first)
        extra_conf = sorted(extra_conf, key=lambda a: len(a[0]), reverse=True)
        context['extra_conf'] = '\n'.join([conf for location, conf in extra_conf])
        return Template(textwrap.dedent("""\
            <VirtualHost{% for ip in ips %} {{ ip }}:{{ port }}{% endfor %}>
                IncludeOptional /etc/apache2/site[s]-override/{{ site_unique_name }}.con[f]
                ServerName {{ server_name }}\
            {% if server_alias %}
                ServerAlias {{ server_alias|join:' ' }}{% endif %}\
            {% if access_log %}
                CustomLog {{ access_log }} common{% endif %}\
            {% if error_log %}
                ErrorLog {{ error_log }}{% endif %}
                SuexecUserGroup {{ user }} {{ group }}\
            {% for line in extra_conf.splitlines %}
                {{ line | safe }}{% endfor %}
            </VirtualHost>
            """)
        ).render(Context(context))
    
    def render_redirect_https(self, context):
        context['port'] = self.HTTP_PORT
        return Template(textwrap.dedent("""
            <VirtualHost{% for ip in ips %} {{ ip }}:{{ port }}{% endfor %}>
                ServerName {{ server_name }}\
            {% if server_alias %}
                ServerAlias {{ server_alias|join:' ' }}{% endif %}\
            {% if access_log %}
                CustomLog {{ access_log }} common{% endif %}\
            {% if error_log %}
                ErrorLog {{ error_log }}{% endif %}
                RewriteEngine On
                RewriteCond %{HTTPS} off
                RewriteRule (.*) https://%{HTTP_HOST}%{REQUEST_URI}
            </VirtualHost>
            """)
        ).render(Context(context))
    
    def save(self, site):
        context = self.get_context(site)
        if context['server_name']:
            apache_conf = '# %(banner)s\n' % context
            if site.protocol in (site.HTTP, site.HTTP_AND_HTTPS):
                apache_conf += self.render_virtual_host(site, context, ssl=False)
            if site.protocol in (site.HTTP_AND_HTTPS, site.HTTPS_ONLY, site.HTTPS):
                apache_conf += self.render_virtual_host(site, context, ssl=True)
            if site.protocol == site.HTTPS_ONLY:
                apache_conf += self.render_redirect_https(context)
            context['apache_conf'] = apache_conf.strip()
            self.append(textwrap.dedent("""
                # Generate Apache site config for %(site_name)s
                read -r -d '' apache_conf << 'EOF' || true
                %(apache_conf)s
                EOF
                {
                    echo -e "${apache_conf}" | diff -N -I'^\s*#' %(sites_available)s -
                } || {
                    echo -e "${apache_conf}" > %(sites_available)s
                    UPDATED_APACHE=1
                }""") % context
            )
        if context['server_name'] and site.active:
            self.append(textwrap.dedent("""
                # Enable site %(site_name)s
                if [[ ! -f %(sites_enabled)s ]]; then
                    a2ensite %(site_unique_name)s.conf
                    UPDATED_APACHE=1
                fi""") % context
            )
        else:
            self.append(textwrap.dedent("""
                # Disable site %(site_name)s
                if [[ -f %(sites_enabled)s ]]; then
                    a2dissite %(site_unique_name)s.conf;
                    UPDATED_APACHE=1
                fi""") % context
            )
    
    def delete(self, site):
        context = self.get_context(site)
        self.append(textwrap.dedent("""
            # Remove site configuration for %(site_name)s
            a2dissite %(site_unique_name)s.conf && UPDATED_APACHE=1
            rm -f %(sites_available)s\
            """) % context
        )
    
    def prepare(self):
        super(Apache2Backend, self).prepare()
        # Coordinate apache restart with php backend in order not to overdo it
        self.append(textwrap.dedent("""
            backend="Apache2Backend"
            echo "$backend" >> /dev/shm/restart.apache2""")
        )
    
    def commit(self):
        """ reload Apache2 if necessary """
        self.append(textwrap.dedent("""
            # Coordinate Apache restart with other concurrent backends (e.g. PHPBackend)
            is_last=0
            mv /dev/shm/restart.apache2 /dev/shm/restart.apache2.locked || {
                sleep 0.2
                mv /dev/shm/restart.apache2 /dev/shm/restart.apache2.locked
            }
            state="$(grep -v "$backend" /dev/shm/restart.apache2.locked)" || is_last=1
            [[ $is_last -eq 0 ]] && {
                echo "$state" | grep -v ' RESTART$' || is_last=1
            }
            if [[ $is_last -eq 1 ]]; then
                if [[ $UPDATED_APACHE -eq 1 || "$state" =~ .*RESTART$ ]]; then
                    if [[ $(service apache2 status) ]]; then
                        service apache2 reload
                    else
                        service apache2 start
                    fi
                fi
                rm /dev/shm/restart.apache2.locked
            else
                echo -n "$state" > /dev/shm/restart.apache2.locked
                if [[ $UPDATED_APACHE -eq 1 ]]; then
                    echo -e "Apache will be restarted by another backend:\\n${state}"
                    echo "$backend RESTART" >> /dev/shm/restart.apache2.locked
                fi
                mv /dev/shm/restart.apache2.locked /dev/shm/restart.apache2
            fi
            # End of coordination
            """)
        )
        super(Apache2Backend, self).commit()
    
    def get_directives(self, directive, context):
        method, args = directive[0], directive[1:]
        try:
            method = getattr(self, 'get_%s_directives' % method)
        except AttributeError:
            context = (self.__class__.__name__, method)
            raise AttributeError("%s does not has suport for '%s' directive." % context)
        return method(context, *args)
    
    def get_content_directives(self, site, context):
        directives = []
        for content in site.content_set.all():
            directive = content.webapp.get_directive()
            self.set_content_context(content, context)
            directives += self.get_directives(directive, context)
        return directives
    
    def get_static_directives(self, context, app_path):
        context['app_path'] = os.path.normpath(app_path % context)
        directive = self.get_location_filesystem_map(context)
        return [
            (context['location'], directive),
        ]
    
    def get_location_filesystem_map(self, context):
        if not context['location']:
            return 'DocumentRoot %(app_path)s' % context
        return 'Alias %(location)s %(app_path)s' % context
    
    def get_fpm_directives(self, context, socket, app_path):
        if ':' in socket:
            # TCP socket
            target = 'fcgi://%(socket)s%(app_path)s/$1'
        else:
            # UNIX socket
            target = 'unix:%(socket)s|fcgi://127.0.0.1%(app_path)s/'
            if context['location']:
                target = 'unix:%(socket)s|fcgi://127.0.0.1%(app_path)s/$1'
        context.update({
            'app_path': os.path.normpath(app_path),
            'socket': socket,
        })
        directives = "ProxyPassMatch ^%(location)s/(.*\.php(/.*)?)$ {target}\n".format(target=target) % context
        directives += self.get_location_filesystem_map(context)
        return [
            (context['location'], directives),
        ]
    
    def get_fcgid_directives(self, context, app_path, wrapper_path):
        context.update({
            'app_path': os.path.normpath(app_path),
            'wrapper_name': os.path.basename(wrapper_path),
        })
        directives = ''
        # This Action trick is used instead of FcgidWrapper because we don't want to define
        # a new fcgid process class each time an app is mounted (num proc limits enforcement).
        if not context['vhost_set_fcgid']:
            # fcgi-bin only needs to be defined once per vhots
            # We assume that all account wrapper paths will share the same dir
            context['wrapper_dir'] = os.path.dirname(wrapper_path)
            context['vhost_set_fcgid'] = True
            directives = textwrap.dedent("""\
                Alias /fcgi-bin/ %(wrapper_dir)s/
                <Location /fcgi-bin/>
                    SetHandler fcgid-script
                    Options +ExecCGI
                </Location>
                """) % context
        directives += self.get_location_filesystem_map(context)
        directives += textwrap.dedent("""
            ProxyPass %(location)s/ !
            <Directory %(app_path)s/>
                AddHandler php-fcgi .php
                Action php-fcgi /fcgi-bin/%(wrapper_name)s
            </Directory>""") % context
        return [
            (context['location'], directives),
        ]
    
    def get_uwsgi_directives(self, context, socket):
        # requires apache2 mod_proxy_uwsgi
        context['socket'] = socket
        directives = "ProxyPass / unix:%(socket)s|uwsgi://" % context
        directives += self.get_location_filesystem_map(context)
        return [
            (context['location'], directives),
        ]
    
    def get_ssl(self, directives):
        cert = directives.get('ssl-cert')
        key = directives.get('ssl-key')
        ca = directives.get('ssl-ca')
        if not (cert and key):
            cert = [settings.WEBSITES_DEFAULT_SSL_CERT]
            key = [settings.WEBSITES_DEFAULT_SSL_KEY]
            ca = [settings.WEBSITES_DEFAULT_SSL_CA]
            if not (cert and key):
                return []
        ssl_config = [
            "SSLEngine on",
            "SSLCertificateFile %s" % cert[0],
            "SSLCertificateKeyFile %s" % key[0],
        ]
        if ca:
           ssl_config.append("SSLCACertificateFile %s" % ca[0])
        return [
            ('', '\n'.join(ssl_config)),
        ]
        
    def get_security(self, directives):
        remove_rules = []
        for values in directives.get('sec-rule-remove', []):
            for rule in values.split():
                sec_rule = "    SecRuleRemoveById %i" % int(rule)
                remove_rules.append(sec_rule)
        security = []
        if remove_rules:
            remove_rules.insert(0, '<IfModule mod_security2.c>')
            remove_rules.append('</IfModule>')
            security.append(('', '\n'.join(remove_rules)))
        for location in directives.get('sec-engine', []):
            sec_rule = textwrap.dedent("""\
                <IfModule mod_security2.c>
                    <Location %s>
                        SecRuleEngine Off
                    </Location>
                </IfModule>""") % location
            security.append((location, sec_rule))
        return security
    
    def get_redirects(self, directives):
        redirects = []
        for redirect in directives.get('redirect', []):
            location, target = redirect.split()
            if re.match(r'^.*[\^\*\$\?\)]+.*$', redirect):
                redirect = "RedirectMatch %s %s" % (location, target)
            else:
                redirect = "Redirect %s %s" % (location, target)
            redirects.append(
                (location, redirect)
            )
        return redirects
    
    def get_proxies(self, directives):
        proxies = []
        for proxy in directives.get('proxy', []):
            proxy = proxy.split()
            location = proxy[0]
            target = proxy[1]
            options = ' '.join(proxy[2:])
            location = normurlpath(location)
            proxy = textwrap.dedent("""\
                ProxyPass {location}/ {target} {options}
                ProxyPassReverse {location}/ {target}""".format(
                    location=location, target=target, options=options)
            )
            proxies.append(
                (location, proxy)
            )
        return proxies
    
    def get_saas(self, directives):
        saas = []
        for name, values in directives.items():
            if name.endswith('-saas'):
                for value in values:
                    context = {
                        'location': normurlpath(value),
                    }
                    directive = settings.WEBSITES_SAAS_DIRECTIVES[name]
                    saas += self.get_directives(directive, context)
        return saas
    
    def get_username(self, site):
        option = site.get_directives().get('user_group')
        if option:
            return option[0]
        return site.get_username()
    
    def get_groupname(self, site):
        option = site.get_directives().get('user_group')
        if option and ' ' in option:
            user, group = option.split()
            return group
        return site.get_groupname()
    
    def get_server_names(self, site):
        server_name = None
        server_alias = []
        for domain in site.domains.all().order_by('name'):
            if not server_name and not domain.name.startswith('*'):
                server_name = domain.name
            else:
                server_alias.append(domain.name)
        return server_name, server_alias
    
    def get_context(self, site):
        base_apache_conf = settings.WEBSITES_BASE_APACHE_CONF
        sites_available = os.path.join(base_apache_conf, 'sites-available')
        sites_enabled = os.path.join(base_apache_conf, 'sites-enabled')
        server_name, server_alias = self.get_server_names(site)
        context = {
            'site': site,
            'site_name': site.name,
            'ips': settings.WEBSITES_DEFAULT_IPS,
            'site_unique_name': site.unique_name,
            'user': self.get_username(site),
            'group': self.get_groupname(site),
            'server_name': server_name,
            'server_alias': server_alias,
            'sites_enabled': "%s.conf" % os.path.join(sites_enabled, site.unique_name),
            'sites_available': "%s.conf" % os.path.join(sites_available, site.unique_name),
            'access_log': site.get_www_access_log_path(),
            'error_log': site.get_www_error_log_path(),
            'banner': self.get_banner(),
        }
        if not context['ips']:
            raise ValueError("WEBSITES_DEFAULT_IPS is empty.")
        return context
    
    def set_content_context(self, content, context):
        content_context = {
            'type': content.webapp.type,
            'location': normurlpath(content.path),
            'app_name': content.webapp.name,
            'app_path': content.webapp.get_path(),
        }
        context.update(content_context)


class Apache2Traffic(ServiceMonitor):
    """
    Parses apache logs,
    looking for the size of each request on the last word of the log line.
    """
    model = 'websites.Website'
    resource = ServiceMonitor.TRAFFIC
    verbose_name = _("Apache 2 Traffic")
    doc_settings = (settings,
        ('WEBSITES_TRAFFIC_IGNORE_HOSTS',)
    )
    def prepare(self):
        super(Apache2Traffic, self).prepare()
        ignore_hosts = '\\|'.join(settings.WEBSITES_TRAFFIC_IGNORE_HOSTS)
        context = {
            'current_date': self.current_date.strftime("%Y-%m-%d %H:%M:%S %Z"),
            'ignore_hosts': '-v "%s"' % ignore_hosts if ignore_hosts else '',
        }
        self.append(textwrap.dedent("""\
            function monitor () {
                OBJECT_ID=$1
                INI_DATE=$(date "+%%Y%%m%%d%%H%%M%%S" -d "$2")
                END_DATE=$(date '+%%Y%%m%%d%%H%%M%%S' -d '%(current_date)s')
                LOG_FILE="$3"
                {
                    { grep %(ignore_hosts)s ${LOG_FILE} || echo -e '\\r'; } \\
                        | awk -v ini="${INI_DATE}" -v end="${END_DATE}" '
                            BEGIN {
                                sum = 0
                                months["Jan"] = "01"
                                months["Feb"] = "02"
                                months["Mar"] = "03"
                                months["Apr"] = "04"
                                months["May"] = "05"
                                months["Jun"] = "06"
                                months["Jul"] = "07"
                                months["Aug"] = "08"
                                months["Sep"] = "09"
                                months["Oct"] = "10"
                                months["Nov"] = "11"
                                months["Dec"] = "12"
                            } {
                                # date = [11/Jul/2014:13:50:41
                                date = substr($4, 2)
                                year = substr(date, 8, 4)
                                month = months[substr(date, 4, 3)];
                                day = substr(date, 1, 2)
                                hour = substr(date, 13, 2)
                                minute = substr(date, 16, 2)
                                second = substr(date, 19, 2)
                                line_date = year month day hour minute second
                                if ( line_date > ini && line_date < end)
                                    sum += $NF
                            } END {
                                print sum
                            }' || [[ $? == 1 ]] && true
                } | xargs echo ${OBJECT_ID}
            }""") % context)
    
    def monitor(self, site):
        context = self.get_context(site)
        self.append('monitor {object_id} "{last_date}" {log_file}'.format(**context))
    
    def get_context(self, site):
        return {
            'log_file': '%s{,.1}' % site.get_www_access_log_path(),
            'last_date': self.get_last_date(site.pk).strftime("%Y-%m-%d %H:%M:%S %Z"),
            'object_id': site.pk,
        }
