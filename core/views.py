# ViewSets define the view behavior.
import json
import os
import re
import sys

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.static import serve
from django.http import QueryDict
from lark.exceptions import LarkError
from rest_framework import viewsets, status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response

from core.models import *
from core.parser import FormulaParser
from core.permissions import OnlySelfUnlessAdminPermission
from core.serializers import *

import requests
from requests.auth import HTTPBasicAuth

# Reset Password
from django.core.mail import EmailMultiAlternatives
from django.dispatch import receiver
from django.template.loader import render_to_string
from django.urls import reverse

from core.notificator import send_notification_by_user
from push_notifications.models import APNSDevice, GCMDevice

from django_rest_passwordreset.signals import reset_password_token_created


class UserViewSet(viewsets.ModelViewSet):
    permission_classes = (IsAuthenticated, OnlySelfUnlessAdminPermission,)
    serializer_class = UserSerializer

    def get_permissions(self):
        if self.request.method == 'POST':
            self.permission_classes = (AllowAny,)
        return super(UserViewSet, self).get_permissions()

    def get_queryset(self):
        if self.request.user.type == UserType.ADMIN.name:
            return User.objects.all()
        else:
            return User.objects.filter(pk=self.request.user.pk)

    @action(detail=True, methods=['post'])
    def set_password(self, request, pk=None):
        user = self.get_object()
        user.set_password(request.data.get("password"))
        user.save()
        return HttpResponse(status=200)

    def perform_destroy(self, instance: User):
        if self.request.user.type == UserType.ADMIN.name or instance == self.request.user:
            if instance.type == UserType.DELETED.name:
                instance.delete()
            else:
                instance.type = UserType.DELETED.name
                instance.is_active = False
                instance.save()


class FlightViewSet(viewsets.ModelViewSet):
    permission_classes = (IsAuthenticated,)
    serializer_class = FlightSerializer

    @action(detail=False)
    def deleted(self, request):
        if self.request.user.type == UserType.ADMIN.name and "HTTP_TARGETUSER" in self.request.META:
            user = User.objects.get(pk=self.request.META["HTTP_TARGETUSER"])
        else:
            user = self.request.user
        serializer = self.get_serializer(
            Flight.objects.filter(user=user, deleted=True), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def make_demo(self, request, pk=None):
        if not request.user.type == UserType.ADMIN.name:
            return Response(status=status.HTTP_403_FORBIDDEN)
        flight: Flight = self.get_object()
        flight.make_demo()
        return Response({})

    @action(detail=True, methods=["delete"])
    def delete_demo(self, request, pk=None):
        if not request.user.type == UserType.ADMIN.name:
            return Response(status=status.HTTP_403_FORBIDDEN)
        flight: Flight = self.get_object()
        flight.unmake_demo(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(
            self.get_queryset().filter(deleted=False))
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def get_queryset(self):
        if self.request.user.type == UserType.ADMIN.name and "HTTP_TARGETUSER" in self.request.META:
            user = User.objects.get(pk=self.request.META["HTTP_TARGETUSER"])
        else:
            user = self.request.user
        return Flight.objects.filter(user=user) | user.demo_flights.all()

    def create(self, request, *args, **kwargs):
        if request.user.type in (UserType.DEMO_USER.name, UserType.DELETED.name):
            return Response(status=403)
        return super(FlightViewSet, self).create(request, *args, **kwargs)

    def perform_create(self, serializer):
        if self.request.user.type == UserType.ADMIN.name and "HTTP_TARGETUSER" in self.request.META:
            serializer.save(user=User.objects.get(
                pk=self.request.META["HTTP_TARGETUSER"]))
        else:
            serializer.save(user=self.request.user)

    def perform_destroy(self, instance: Flight):
        if instance.is_demo:
            # Remove demo flight ONLY FOR USER!
            self.request.user.demo_flights.remove(instance)
        elif self.request.user.type == UserType.ADMIN.name or instance.user == self.request.user:
            if instance.deleted:
                instance.delete()
            else:
                instance.deleted = True
                instance.save()


class ArtifactViewSet(viewsets.ModelViewSet):
    permission_classes = (IsAuthenticated,)
    serializer_class = ArtifactSerializer

    def get_queryset(self):
        return Artifact.objects.all()


class UserProjectViewSet(viewsets.ModelViewSet):
    permission_classes = (IsAuthenticated,)
    serializer_class = UserProjectSerializer

    @action(detail=False)
    def deleted(self, request):
        if self.request.user.type == UserType.ADMIN.name and "HTTP_TARGETUSER" in self.request.META:
            user = User.objects.get(pk=self.request.META["HTTP_TARGETUSER"])
        else:
            user = self.request.user
        serializer = self.get_serializer(
            UserProject.objects.filter(user=user, deleted=True), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def make_demo(self, request, pk=None):
        if not request.user.type == UserType.ADMIN.name:
            return Response(status=status.HTTP_403_FORBIDDEN)
        project: UserProject = self.get_object()
        project.is_demo = True
        project.user = None
        for user in User.objects.all():
            user.demo_projects.add(project)
        for flight in project.flights.all():
            flight.make_demo()
        project.save()
        return Response({})

    @action(detail=True, methods=["delete"])
    def delete_demo(self, request, pk=None):
        if not request.user.type == UserType.ADMIN.name:
            return Response(status=status.HTTP_403_FORBIDDEN)
        project: UserProject = self.get_object()
        project.is_demo = False
        project.user = request.user
        project.demo_users.clear()
        for flight in project.flights.all():
            flight.unmake_demo(request.user)
        project.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(
            self.get_queryset().filter(deleted=False))
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def get_queryset(self):
        if self.request.user.type == UserType.ADMIN.name and "HTTP_TARGETUSER" in self.request.META:
            user = User.objects.get(pk=self.request.META["HTTP_TARGETUSER"])
        else:
            user = self.request.user
        return UserProject.objects.filter(user=user) | user.demo_projects.all()

    def create(self, request, *args, **kwargs):
        if request.user.type in (UserType.DEMO_USER.name, UserType.DELETED.name):
            return Response(status=403)
        return super(UserProjectViewSet, self).create(request, *args, **kwargs)

    def perform_create(self, serializer):
        all_flights = [Flight.objects.get(
            uuid=uuid) for uuid in self.request.POST.getlist("flights")]
        if self.request.user.type == UserType.ADMIN.name and "HTTP_TARGETUSER" in self.request.META:
            target_user = User.objects.get(
                pk=self.request.META["HTTP_TARGETUSER"])
        else:
            target_user = self.request.user
        serializer.save(user=target_user, flights=[f for f in all_flights if f.user == target_user])

    def perform_destroy(self, instance: UserProject):
        if instance.is_demo:
            # Remove demo flight ONLY FOR USER!
            self.request.user.demo_projects.remove(instance)
        elif self.request.user.type == UserType.ADMIN.name or instance.user == self.request.user:
            if instance.deleted:
                instance.delete()
            else:
                instance.deleted = True
                instance.save()


@csrf_exempt
def upload_images(request, uuid):
    flight = get_object_or_404(Flight, uuid=uuid)
    user = Token.objects.get(key=request.headers["Authorization"][6:]).user
    if not user.type == UserType.ADMIN.name and not flight.user == user:
        return HttpResponse(status=403)

    files = []
    filenames = []
    # save temp files to disk before uploading
    for f in request.FILES.getlist("images"):
        path = default_storage.save('tmp/' + f.name, ContentFile(f.read()))
        tmp_file = os.path.join(settings.MEDIA_ROOT, path)
        filenames.append(tmp_file)
        files.append(('images', open(tmp_file, "rb")))
    # upload files to NodeODM server
    r = requests.post(
        "http://container-nodeodm:3000/task/new/upload/" + str(flight.uuid), files=files)
    if r.status_code != 200:
        return HttpResponse(status=500)
    for f in filenames:  # delete temp files from disk
        os.remove(f)

    # start processing Flight on NodeODM
    r = requests.post(
        "http://container-nodeodm:3000/task/new/commit/" + str(flight.uuid))
    if r.status_code != 200:
        return HttpResponse(status=500)

    flight.state = FlightState.PROCESSING.name
    flight.save()  # change Flight state to PROCESSING

    return HttpResponse()


@csrf_exempt
def webhook_processing_complete(request):
    data = json.loads(request.body.decode("utf-8"))
    flight = Flight.objects.get(uuid=data["uuid"])
    username = flight.user.username

    if data["status"]["code"] == 30:
        flight.state = FlightState.ERROR.name
    elif data["status"]["code"] == 40:
        success_message = "El análisis de su vuelo ha terminado entre para ver los resultados"
        flight.state = FlightState.COMPLETE.name
        send_notification_by_user(username, success_message)
    elif data["status"]["code"] == 50:
        flight.state = FlightState.CANCELED.name
    flight.processing_time = data.get("processingTime", 0)
    flight.save()

    if flight.state == FlightState.COMPLETE.name:
        flight.try_create_thumbnail()
        print("Creating PNG ortho...")
        flight.try_create_png_ortho()
        flight.try_create_png_dsm()
        flight.try_create_dsm_colorbar()
        print("Creating annotated PNG ortho...")
        flight.try_create_annotated_png_ortho()
        # _try_create_thumbnail must have been invoked here!
        flight.create_geoserver_workspace_and_upload_geotiff()
    return HttpResponse()


def download_artifact(request, uuid, artifact):
    flight = get_object_or_404(Flight, uuid=uuid)

    filepath = flight.get_disk_path()
    if artifact == "orthomosaic.png":
        filepath += "/odm_orthophoto/odm_orthophoto.png"
    elif artifact == "orthomosaic.annotated.png":
        filepath += "/odm_orthophoto/odm_orthophoto_annotated.png"
    elif artifact == "orthomosaic.tiff":
        filepath += "/odm_orthophoto/odm_orthophoto.tif"
    elif artifact == "dsm.png":
        filepath += "/odm_dem/dsm_colored_hillshade.png"
    elif artifact == "dsm_colorbar.png":
        filepath += "/odm_dem/colorbar.png"
    elif artifact == "3dmodel":
        filepath += "/odm_meshing/odm_mesh.ply"
    elif artifact == "3dmodel_texture":
        filepath += "/odm_texturing/odm_textured_model.obj"
    elif artifact == "thumbnail":
        filepath = "./tmp/" + str(uuid) + "_thumbnail.png"
    elif artifact == "pointcloud.ply":
        filepath += "/odm_filterpoints/point_cloud.ply"
    elif artifact == "report.pdf":
        filepath = flight.create_report(request.GET)
    else:
        raise Http404
    return serve(request, os.path.basename(filepath), os.path.dirname(filepath))


def download_artifact_movil(request, uuid, options, artifact):
    flight = get_object_or_404(Flight, uuid=uuid)
    content = {}
    option_values = {
        "c": 'pointcloud',
        "m": 'orthomosaic',
        "g": 'generaldata',
        "n": 'ndviortho',
        "3": '3dmodel'
    }

    for i in options:
        content[option_values[i]] = True

    dict_content = QueryDict('', mutable=True)
    dict_content.update(content)

    filepath = flight.get_disk_path()
    if artifact == "report.pdf":
        filepath = flight.create_report_movil(dict_content)
    else:
        raise Http404
    return serve(request, os.path.basename(filepath), os.path.dirname(filepath))


@csrf_exempt
def upload_vectorfile(request, uuid):
    from django.core.files.uploadedfile import UploadedFile

    datatype = request.POST.get("datatype", "shp")
    project = UserProject.objects.get(pk=uuid)
    # shapefile is an array with files [X.shp, X.shx, X.dbf], in some order
    if datatype == "shp":
        file: UploadedFile = request.FILES.getlist(
            "file")[0]  # gets name X.Y (cannot guarantee Y)
    elif datatype == "kml":
        file: UploadedFile = request.FILES["file"]
    # remove extension to get only X
    file_name = ".".join(file.name.split(".")[:-1]).replace(" ", "")
    project.artifacts.create(
        name=file_name, type=ArtifactType.SHAPEFILE.name, title=request.POST["title"])

    # Write file(s) to disk on project folder
    os.makedirs(project.get_disk_path() + "/" + file_name)
    for file in request.FILES.getlist("file") if datatype == "shp" else [request.FILES["file"]]:
        extension = file.name.split(".")[-1]
        with open(project.get_disk_path() + "/" + file_name + "/" + file_name + "." + extension, "wb") as f:
            for chunk in file.chunks():
                f.write(chunk)

    if datatype == "kml":
        original_dir = os.getcwd()
        os.chdir(project.get_disk_path() + "/" + file_name)
        os.system('ogr2ogr -f "ESRI Shapefile" "{0}.shp" "{0}.kml"'.format(file_name))
        os.chdir(original_dir)

    GEOSERVER_BASE_URL = "http://container-nginx/geoserver/geoserver/rest/workspaces/"

    requests.put(
        GEOSERVER_BASE_URL + project._get_geoserver_ws_name() + "/datastores/" +
        file_name + "/"
                    "external.shp",
        headers={"Content-Type": "text/plain"},
        data="file:///media/USB/" +
             str(project.uuid) + "/" + file_name + "/" + file_name + ".shp",
        auth=HTTPBasicAuth('admin', 'geoserver'))

    requests.put(
        GEOSERVER_BASE_URL + project._get_geoserver_ws_name() + "/datastores/" +
        file_name + "/featuretypes/" + file_name + ".json",
        headers={"Content-Type": "application/json"},
        data='{"featureType": {"enabled": true, "srs": "EPSG:4326" }}',
        auth=HTTPBasicAuth('admin', 'geoserver'))
    return HttpResponse(status=201)


@csrf_exempt
def upload_geotiff(request, uuid):
    from django.core.files.uploadedfile import UploadedFile

    project = UserProject.objects.get(pk=uuid)
    file: UploadedFile = request.FILES.get("geotiff")  # file is called X.tiff
    geotiff_name = ".".join(file.name.split(
        ".")[:-1])  # Remove extension, get X
    project.artifacts.create(
        name=geotiff_name, type=ArtifactType.ORTHOMOSAIC.name, title=request.POST["title"])

    # Write file to disk on project folder
    os.makedirs(project.get_disk_path() + "/" + geotiff_name)
    with open(project.get_disk_path() + "/" + geotiff_name + "/" + geotiff_name + ".tiff", "wb") as f:
        for chunk in file.chunks():
            f.write(chunk)

    GEOSERVER_BASE_URL = "http://container-nginx/geoserver/geoserver/rest/workspaces/"

    requests.put(
        GEOSERVER_BASE_URL + project._get_geoserver_ws_name() + "/coveragestores/" +
        geotiff_name + "/"
                       "external.geotiff",
        headers={"Content-Type": "text/plain"},
        data="file:///media/USB/" +
             str(project.uuid) + "/" + geotiff_name + "/" + geotiff_name + ".tiff",
        auth=HTTPBasicAuth('admin', 'geoserver'))

    requests.put(
        GEOSERVER_BASE_URL + project._get_geoserver_ws_name() + "/coveragestores/" +
        geotiff_name + "/coverages/" + geotiff_name + ".json",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"coverage": {
            "enabled": True,
            "parameters": {"entry": [
                {"string": ["InputTransparentColor", "#000000"]},
                {"string": ["SUGGESTED_TILE_SIZE", "512,512"]}
            ]}
        }}),
        auth=HTTPBasicAuth('admin', 'geoserver'))
    return HttpResponse(status=201)


def preview_flight_url(request, uuid):
    flight = get_object_or_404(Flight, uuid=uuid)

    ans = requests.get(
        "http://container-nginx/geoserver/geoserver/rest/workspaces/" + flight._get_geoserver_ws_name() +
        "/coveragestores/ortho/coverages/odm_orthophoto.json",
        auth=HTTPBasicAuth('admin', 'geoserver')).json()
    bbox = ans["coverage"]["nativeBoundingBox"]
    base = "/geoserver/geoserver/" + flight._get_geoserver_ws_name() + \
           "/wms?service=WMS&version=1.1.0&request=GetMap&layers=" + flight._get_geoserver_ws_name() + \
           ":odm_orthophoto&styles=&bbox=" + \
           ','.join(map(str, (bbox["minx"], bbox["miny"], bbox["maxx"], bbox["maxy"]))) + \
           "&width=1000&height=1000&srs=EPSG:32617&format=application/openlayers"

    return JsonResponse({"url": base, "bbox": bbox, "srs": ans["coverage"]["srs"]})


@csrf_exempt
def check_formula(request):
    return HttpResponse(status=200 if FormulaParser().is_valid(request.POST["formula"]) else 400)


@csrf_exempt
def create_raster_index(request, uuid):
    project = UserProject.objects.get(uuid=uuid)

    if not project.all_flights_multispectral():
        return HttpResponse("Not all flights are multispectral!", status=400)

    data = json.loads(request.body.decode('utf-8'))
    print(data)

    index = data.get("index", "custom")
    clean_index = re.sub(r"[^a-z0-9_-]", "", index)
    formula = data.get("formula", "")

    print("FORMULA->" + formula)
    for flight in project.flights.all():
        flight.create_index_raster(clean_index, formula)
    project._create_index_datastore(clean_index)
    project.artifacts.create(
        name=clean_index, type=ArtifactType.INDEX.name, title=clean_index.upper())
    return HttpResponse(status=200)


@xframe_options_exempt
def mapper(request, uuid):
    project = UserProject.objects.get(uuid=uuid)

    return render(request, "geoext/examples/tree/panel.html",
                  {"project_name": project.name,
                   "project_notes": project.description,
                   "project_geoserver_path": project._get_geoserver_ws_name(),
                   "upload_shapefiles_path": "/#/projects/" + str(project.uuid) + "/upload/shapefile",
                   "upload_geotiff_path": "/#/projects/" + str(project.uuid) + "/upload/geotiff",
                   "upload_new_index_path": "/#/projects/" + str(project.uuid) + "/upload/index",
                   "is_multispectral": project.all_flights_multispectral(),
                   "is_demo": project.is_demo,
                   "uuid": project.uuid,
                   "flights": project.flights.all().order_by("date")})


def mapper_bbox(request, uuid):
    project = UserProject.objects.get(uuid=uuid)

    ans = requests.get(
        "http://container-nginx/geoserver/geoserver/rest/workspaces/" + project._get_geoserver_ws_name() +
        "/coveragestores/mainortho/coverages/mainortho.json",
        auth=HTTPBasicAuth('admin', 'geoserver')).json()

    return JsonResponse({"bbox": ans["coverage"]["nativeBoundingBox"], "srs": ans["coverage"]["srs"]})


def mapper_artifacts(request, uuid):
    project = UserProject.objects.get(uuid=uuid)

    return JsonResponse({"artifacts": [
        {"name": art.title,
         "layer": project._get_geoserver_ws_name() + ":" + art.name,
         "type": art.type}
        for art in project.artifacts.all()
    ]})


def mapper_indices(request, uuid):
    project = UserProject.objects.get(uuid=uuid)

    return JsonResponse({"indices": [
        {"name": art.name, "title": art.title,
         "layer": project._get_geoserver_ws_name() + ":" + art.name}
        for art in project.artifacts.all()
        if art.type == ArtifactType.INDEX.name
    ]})


def mapper_paneljs(request):
    filepath = "./templates/geoext/examples/tree/panel.js"
    return serve(request, os.path.basename(filepath), os.path.dirname(filepath))


def mapper_ticks(request, num_ticks):
    filepath = "./templates/geoext/examples/tree/" + str(num_ticks) + "ticks.png"
    return serve(request, os.path.basename(filepath), os.path.dirname(filepath))


def mapper_ol(request, path):
    filepath = "./templates/geoext/examples/lib/ol/" + path
    return serve(request, os.path.basename(filepath), os.path.dirname(filepath))


def mapper_src(request, path):
    filepath = "./templates/geoext/src/" + path
    return serve(request, os.path.basename(filepath), os.path.dirname(filepath))


@receiver(reset_password_token_created)
def password_reset_token_created(sender, instance, reset_password_token, *args, **kwargs):
    # send an e-mail to the user
    context = {
        'current_user': reset_password_token.user,
        'username': reset_password_token.user.username,
        'email': reset_password_token.user.email,
        # 'reset_password_url': "http://localhost/#/restorePassword/reset?token={}".format(reset_password_token.key)
        'reset_password_url': "http://droneapp.ngrok.io/#/restorePassword/reset?token={}".format(
            reset_password_token.key)
    }

    # render email text
    email_html_message = render_to_string(
        'email/user_reset_password.html', context)
    email_plaintext_message = render_to_string(
        'email/user_reset_password.txt', context)

    msg = EmailMultiAlternatives(
        # title:
        "Password Reset for {title}".format(title="AgroSmart"),
        # message:
        email_plaintext_message,
        # from:
        "AgroSmart Team",
        # to:
        [reset_password_token.user.email]
    )
    msg.attach_alternative(email_html_message, "text/html")
    msg.send()


@csrf_exempt
def save_push_device(request, device):
    user_name = request.POST["username"]

    try:
        the_user = User.objects.get(username__iexact=user_name)
    except User.DoesNotExist:
        the_user = None

    if not the_user:
        return HttpResponse(status=400)
    token = request.POST['token']

    try:
        gcm_user = GCMDevice.objects.get(user__id__icontains=the_user.id)
    except GCMDevice.DoesNotExist:
        gcm_user = None

    if gcm_user:
        return HttpResponse(status=200)

    if device == "ios":
        apns_device = APNSDevice.objects.create(
            registration_id=token, user=the_user)
    if device == "android":
        fcm_device = GCMDevice.objects.create(
            registration_id=token, cloud_message_type="FCM", user=the_user)

    return HttpResponse(status=200)


class BlockCriteriaViewSet(viewsets.ModelViewSet):
    # permission_classes = (IsAuthenticated,)
    serializer_class = BlockCriteriaSerializer

    def get_queryset(self):
        return BlockCriteria.objects.all()
