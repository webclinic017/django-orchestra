from rest_framework import viewsets, mixins
from rest_framework.decorators import action
from rest_framework.response import Response

from orchestra.api import router

from .models import Ticket, Queue
from .serializers import TicketSerializer, QueueSerializer



class TicketViewSet(viewsets.ModelViewSet):
    model = Ticket
    serializer_class = TicketSerializer
    
    @action()
    def mark_as_read(self, request, pk=None):
        ticket = self.get_object()
        ticket.mark_as_read()
        return Response({'status': 'Ticket marked as readed'})
    
    @action()
    def mark_as_unread(self, request, pk=None):
        ticket = self.get_object()
        ticket.mark_as_unread()
        return Response({'status': 'Ticket marked as unreaded'})
    
    def get_queryset(self):
        qs = super(TicketViewSet, self).get_queryset()
        return qs.filter(creator__account=self.request.user.account_id)


class QueueViewSet(mixins.ListModelMixin,
                   mixins.RetrieveModelMixin,
                   viewsets.GenericViewSet):
    model = Queue
    serializer_class = QueueSerializer


router.register(r'tickets', TicketViewSet)
router.register(r'ticket-queues', QueueViewSet)
