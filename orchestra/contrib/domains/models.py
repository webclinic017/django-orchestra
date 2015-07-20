from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import ungettext, ugettext_lazy as _

from orchestra.core.validators import validate_ipv4_address, validate_ipv6_address, validate_ascii
from orchestra.utils.python import AttrDict

from . import settings, validators, utils


class Domain(models.Model):
    name = models.CharField(_("name"), max_length=256, unique=True,
        help_text=_("Domain or subdomain name."),
        validators=[
            validators.validate_domain_name,
            validators.validate_allowed_domain
        ])
    account = models.ForeignKey('accounts.Account', verbose_name=_("Account"), blank=True,
        related_name='domains', help_text=_("Automatically selected for subdomains."))
    top = models.ForeignKey('domains.Domain', null=True, related_name='subdomain_set',
        editable=False)
    serial = models.IntegerField(_("serial"), default=utils.generate_zone_serial, editable=False,
        help_text=_("A revision number that changes whenever this domain is updated."))
    refresh = models.CharField(_("refresh"), max_length=16, blank=True,
        validators=[validators.validate_zone_interval],
        help_text=_("The time a secondary DNS server waits before querying the primary DNS "
                    "server's SOA record to check for changes. When the refresh time expires, "
                    "the secondary DNS server requests a copy of the current SOA record from "
                    "the primary. The primary DNS server complies with this request. "
                    "The secondary DNS server compares the serial number of the primary DNS "
                    "server's current SOA record and the serial number in it's own SOA record. "
                    "If they are different, the secondary DNS server will request a zone "
                    "transfer from the primary DNS server. "
                    "The default value is <tt>%s</tt>.") % settings.DOMAINS_DEFAULT_REFRESH)
    retry = models.CharField(_("retry"), max_length=16, blank=True,
        validators=[validators.validate_zone_interval],
        help_text=_("The time a secondary server waits before retrying a failed zone transfer. "
                    "Normally, the retry time is less than the refresh time. "
                    "The default value is <tt>%s</tt>.") % settings.DOMAINS_DEFAULT_RETRY)
    expire = models.CharField(_("expire"), max_length=16, blank=True,
        validators=[validators.validate_zone_interval],
        help_text=_("The time that a secondary server will keep trying to complete a zone "
                    "transfer. If this time expires prior to a successful zone transfer, "
                    "the secondary server will expire its zone file. This means the secondary "
                    "will stop answering queries. "
                    "The default value is <tt>%s</tt>.") % settings.DOMAINS_DEFAULT_EXPIRE)
    min_ttl = models.CharField(_("min TTL"), max_length=16, blank=True,
        validators=[validators.validate_zone_interval],
        help_text=_("The minimum time-to-live value applies to all resource records in the "
                    "zone file. This value is supplied in query responses to inform other "
                    "servers how long they should keep the data in cache. "
                    "The default value is <tt>%s</tt>.") % settings.DOMAINS_DEFAULT_MIN_TTL)
    
    def __str__(self):
        return self.name
    
    @classmethod
    def get_parent_domain(cls, name, top=False):
        """ get the next domain on the chain """
        split = name.split('.')
        parent = None
        for i in range(1, len(split)-1):
            name = '.'.join(split[i:])
            domain = Domain.objects.filter(name=name)
            if domain:
                parent = domain.get()
                if not top:
                    return parent
        return parent
    
    @property
    def origin(self):
        return self.top or self
    
    @property
    def is_top(self):
        # don't cache, don't replace by top_id
        return not bool(self.top)
    
    @property
    def subdomains(self):
        return Domain.objects.filter(name__regex='\.%s$' % self.name)
    
    def clean(self):
        self.name = self.name.lower()
    
    def save(self, *args, **kwargs):
        """ create top relation """
        update = False
        if not self.pk:
            top = self.get_parent(top=True)
            if top:
                self.top = top
                self.account_id = self.account_id or top.account_id
            else:
                update = True
        super(Domain, self).save(*args, **kwargs)
        if update:
            for domain in self.subdomains.exclude(pk=self.pk):
                # queryset.update() is not used because we want to trigger backend to delete ex-topdomains
                domain.top = self
                domain.save(update_fields=['top'])
    
    def get_description(self):
        if self.is_top:
            num = self.subdomains.count()
            return ungettext(
                _("top domain with one subdomain"),
                _("top domain with %d subdomains") % num,
                num)
        return _("subdomain")
    
    def get_absolute_url(self):
        return 'http://%s' % self.name
    
    def get_declared_records(self):
        """ proxy method, needed for input validation, see helpers.domain_for_validation """
        return self.records.all()
    
    def get_subdomains(self):
        """ proxy method, needed for input validation, see helpers.domain_for_validation """
        return self.origin.subdomain_set.all().prefetch_related('records')
    
    def get_parent(self, top=False):
        return self.get_parent_domain(self.name, top=top)
    
    def render_zone(self):
        origin = self.origin
        zone = origin.render_records()
        tail = []
        for subdomain in origin.get_subdomains():
            if subdomain.name.startswith('*'):
                # This subdomains needs to be rendered last in order to avoid undesired matches
                tail.append(subdomain)
            else:
                zone += subdomain.render_records()
        for subdomain in sorted(tail, key=lambda x: len(x.name), reverse=True):
            zone += subdomain.render_records()
        return zone.strip()
    
    def refresh_serial(self):
        """ Increases the domain serial number by one """
        serial = utils.generate_zone_serial()
        if serial <= self.serial:
            num = int(str(self.serial)[8:]) + 1
            if num >= 99:
                raise ValueError('No more serial numbers for today')
            serial = str(self.serial)[:8] + '%.2d' % num
            serial = int(serial)
        self.serial = serial
        self.save(update_fields=['serial'])
    
    def get_records(self):
        types = {}
        records = utils.RecordStorage()
        for record in self.get_declared_records():
            types[record.type] = True
            if record.type == record.SOA:
                # Update serial and insert at 0
                value = record.value.split()
                value[2] = str(self.serial)
                records.insert(0, AttrDict(
                    type=record.SOA,
                    ttl=record.get_ttl(),
                    value=' '.join(value)
                ))
            else:
                records.append(AttrDict(
                    type=record.type,
                    ttl=record.get_ttl(),
                    value=record.value
                ))
        if self.is_top:
            if Record.NS not in types:
                for ns in settings.DOMAINS_DEFAULT_NS:
                    records.append(AttrDict(
                        type=Record.NS,
                        value=ns
                    ))
            if Record.SOA not in types:
                soa = [
                    "%s." % settings.DOMAINS_DEFAULT_NAME_SERVER,
                    utils.format_hostmaster(settings.DOMAINS_DEFAULT_HOSTMASTER),
                    str(self.serial),
                    self.refresh or settings.DOMAINS_DEFAULT_REFRESH,
                    self.retry or settings.DOMAINS_DEFAULT_RETRY,
                    self.expire or settings.DOMAINS_DEFAULT_EXPIRE,
                    self.min_ttl or settings.DOMAINS_DEFAULT_MIN_TTL,
                ]
                records.insert(0, AttrDict(
                    type=Record.SOA,
                    value=' '.join(soa)
                ))
        has_a = Record.A in types
        has_aaaa = Record.AAAA in types
        is_host = self.is_top or not types or has_a or has_aaaa
        if is_host:
            if Record.MX not in types:
                for mx in settings.DOMAINS_DEFAULT_MX:
                    records.append(AttrDict(
                        type=Record.MX,
                        value=mx
                    ))
            # A and AAAA point to the same default host
            if not has_a and not has_aaaa:
                default_a = settings.DOMAINS_DEFAULT_A
                if default_a:
                    records.append(AttrDict(
                        type=Record.A,
                        value=default_a
                    ))
                default_aaaa = settings.DOMAINS_DEFAULT_AAAA
                if default_aaaa:
                    records.append(AttrDict(
                        type=Record.AAAA,
                        value=default_aaaa
                    ))
        return records
    
    def render_records(self):
        result = ''
        for record in self.get_records():
            name = '{name}.{spaces}'.format(
                name=self.name,
                spaces=' ' * (37-len(self.name))
            )
            ttl = record.get('ttl', settings.DOMAINS_DEFAULT_TTL)
            ttl = '{spaces}{ttl}'.format(
                spaces=' ' * (7-len(ttl)),
                ttl=ttl
            )
            type = '{type} {spaces}'.format(
                type=record.type,
                spaces=' ' * (7-len(record.type))
            )
            result += '{name} {ttl} IN {type} {value}\n'.format(
                name=name,
                ttl=ttl,
                type=type,
                value=record.value
            )
        return result
    
    def has_default_mx(self):
        records = self.get_records()
        for record in records.by_type('MX'):
            for default in settings.DOMAINS_DEFAULT_MX:
                if record.value.endswith(' %s' % default.split()[-1]):
                    return True
        return False


class Record(models.Model):
    """ Represents a domain resource record  """
    MX = 'MX'
    NS = 'NS'
    CNAME = 'CNAME'
    A = 'A'
    AAAA = 'AAAA'
    SRV = 'SRV'
    TXT = 'TXT'
    SOA = 'SOA'
    
    TYPE_CHOICES = (
        (MX, "MX"),
        (NS, "NS"),
        (CNAME, "CNAME"),
        (A, _("A (IPv4 address)")),
        (AAAA, _("AAAA (IPv6 address)")),
        (SRV, "SRV"),
        (TXT, "TXT"),
        (SOA, "SOA"),
    )
    
    VALIDATORS = {
        MX: validators.validate_mx_record,
        NS: validators.validate_zone_label,
        A: validate_ipv4_address,
        AAAA: validate_ipv6_address,
        CNAME: validators.validate_zone_label,
        TXT: validate_ascii,
        SRV: validators.validate_srv_record,
        SOA: validators.validate_soa_record,
    }
    
    domain = models.ForeignKey(Domain, verbose_name=_("domain"), related_name='records')
    ttl = models.CharField(_("TTL"), max_length=8, blank=True,
        help_text=_("Record TTL, defaults to %s") % settings.DOMAINS_DEFAULT_TTL,
        validators=[validators.validate_zone_interval])
    type = models.CharField(_("type"), max_length=32, choices=TYPE_CHOICES)
    value = models.CharField(_("value"), max_length=256,
        help_text=_("MX, NS and CNAME records sould end with a dot."))
    
    def __str__(self):
        return "%s %s IN %s %s" % (self.domain, self.get_ttl(), self.type, self.value)
    
    def clean(self):
        """ validates record value based on its type """
        # validate value
        if self.type != self.TXT:
            self.value = self.value.lower().strip()
        if self.type:
            try:
                self.VALIDATORS[self.type](self.value)
            except ValidationError as error:
                raise ValidationError({
                    'value': error,
                })
    
    def get_ttl(self):
        return self.ttl or settings.DOMAINS_DEFAULT_TTL
