from rest_framework import serializers

from core.models import *


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["pk", 'username', 'email', 'is_staff']


class FlightSerializer(serializers.ModelSerializer):
    nodeodm_info = serializers.SerializerMethodField()

    def get_nodeodm_info(self, flight):
        return flight.get_nodeodm_info()

    class Meta:
        model = Flight
        fields = ["uuid", "name", "user", "date", "camera", "annotations", "state", "nodeodm_info", "processing_time",
                  "is_demo"]


class ArtifactSerializer(serializers.ModelSerializer):
    class Meta:
        model = Artifact
        fields = ["pk", "type", "project", "name", "type"]


class UserProjectSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        default=serializers.CurrentUserDefault())
    flights = serializers.PrimaryKeyRelatedField(many=True,
                                                 queryset=Flight.objects.all())
    artifacts = serializers.PrimaryKeyRelatedField(many=True,
                                                   queryset=Artifact.objects.all())

    def create(self, validated_data):
        flights = validated_data.pop("flights")
        artifacts = validated_data.pop("artifacts")
        proj = UserProject.objects.create(**validated_data)
        proj.flights.set(flights)
        proj.artifacts.set(artifacts)
        proj._create_geoserver_proj_workspace()
        return proj

    class Meta:
        model = UserProject
        fields = ['uuid', 'user', 'flights', 'artifacts', "name", "description"]
